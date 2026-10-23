"""One-way, recoverable mirror of the owner's journal. No model calls."""
import asyncio
import copy
import hashlib
import json
import logging
import re
from datetime import date, timedelta
from urllib.parse import quote

from .db import SheetSync, utcnow
from .service import Journal


HEADERS = ['Дата', 'Упражнение', 'Подход', 'Вес, кг', 'Повторения', 'Время, мин', 'RPE',
           'Зал', 'Тренер', 'Неделя программы', 'Тренировка программы', 'Примечание',
           'Проверить название', 'Код упражнения', 'ID тренировки', 'ID подхода']


def sheet_cell(value):
    if value is None or value == '':
        return {}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {'userEnteredValue': {'numberValue': value}}
    # Explicit stringValue prevents formula injection, including a leading '='.
    return {'userEnteredValue': {'stringValue': str(value)}}


def cell_rows(rows):
    result = [{'values': [sheet_cell(value) for value in HEADERS]}]
    epoch = date(1899, 12, 30)
    for row in rows:
        values = [(date.fromisoformat(row[0]) - epoch).days, *row[1:]]
        result.append({'values': [sheet_cell(value) for value in values]})
    return result


def update_requests(rows, sheet):
    """Single atomic update replaces owned values, including removed trailing rows."""
    props = sheet['properties']
    sheet_id = props['sheetId']
    grid = props['gridProperties']
    row_count = max(grid['rowCount'], len(rows) + 1)
    column_count = max(grid['columnCount'], len(HEADERS))
    requests = []
    if row_count != grid['rowCount'] or column_count != grid['columnCount']:
        requests.append({'updateSheetProperties': {'properties': {'sheetId': sheet_id,
            'gridProperties': {'rowCount': row_count, 'columnCount': column_count}},
            'fields': 'gridProperties.rowCount,gridProperties.columnCount'}})
    requests.append({'updateCells': {
        'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': row_count,
                  'startColumnIndex': 0, 'endColumnIndex': len(HEADERS)},
        'rows': cell_rows(rows), 'fields': 'userEnteredValue'}})
    current_filter = copy.deepcopy(sheet.get('basicFilter', {}))
    current_filter['range'] = {'sheetId': sheet_id, 'startRowIndex': 0,
                               'endRowIndex': len(rows) + 1, 'startColumnIndex': 0,
                               'endColumnIndex': len(HEADERS)}
    requests.append({'setBasicFilter': {'filter': current_filter}})
    # Extend number formats to new records without changing user column widths.
    if rows:
        requests.append({'repeatCell': {'range': {'sheetId': sheet_id, 'startRowIndex': 1,
            'endRowIndex': len(rows) + 1, 'startColumnIndex': 0, 'endColumnIndex': 1},
            'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}}},
            'fields': 'userEnteredFormat.numberFormat'}})
    return requests


class GoogleSheets:
    def __init__(self, settings):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,}', settings.google_spreadsheet_id):
            raise ValueError('Некорректный ID Google-таблицы')
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        credentials = Credentials.from_service_account_file(str(settings.google_credentials_file),
            scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session = AuthorizedSession(credentials)
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/' + settings.google_spreadsheet_id
        self.sheet_id = settings.google_sheet_tab_id

    def sheet(self):
        response = self.session.get(self.base, params={
            'fields': 'sheets(properties(sheetId,title,gridProperties),basicFilter)'}, timeout=30)
        response.raise_for_status()
        sheet = next((s for s in response.json()['sheets']
                      if s['properties']['sheetId'] == self.sheet_id), None)
        if not sheet or sheet['properties']['title'] != 'Тренировки':
            raise ValueError('Ожидаемый лист «Тренировки» не найден')
        title = "'" + sheet['properties']['title'].replace("'", "''") + "'"
        header = self.session.get(self.base + '/values/' + quote(title + '!A1:P1', safe=''), timeout=30)
        header.raise_for_status()
        if header.json().get('values') != [HEADERS]:
            raise ValueError('Заголовки листа изменились. Синхронизация остановлена для проверки')
        return sheet

    def update(self, requests):
        payload = json.dumps({'requests': requests}, ensure_ascii=False, separators=(',', ':')).encode()
        response = self.session.post(self.base + ':batchUpdate', data=payload,
                                     headers={'Content-Type': 'application/json; charset=utf-8'}, timeout=90)
        response.raise_for_status()

    def close(self):
        self.session.close()


class SheetMirror:
    def __init__(self, settings, db, transport=None):
        self.settings, self.db, self.transport = settings, db, transport
        self.key = 'owner:' + settings.google_spreadsheet_id + ':' + str(settings.google_sheet_tab_id)

    def sync_once(self, force=False):
        if not self.settings.google_spreadsheet_id:
            return {'enabled': False}
        rows = Journal(self.db, 'owner').history_rows()
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        with self.db.session() as s:
            state = s.get(SheetSync, self.key)
            # Periodic reconciliation repairs accidental mirror edits even without DB changes.
            if not force and state and state.digest == digest and state.synced_at > utcnow() - timedelta(hours=6):
                return {'enabled': True, 'changed': False, 'rows': len(rows)}
        transport = self.transport or GoogleSheets(self.settings)
        try:
            sheet = transport.sheet()
            transport.update(update_requests(rows, sheet))
        finally:
            if self.transport is None:
                transport.close()
        # Advance state only after Google acknowledges the entire atomic update.
        # A lost response is safe to retry: a full replacement never appends duplicates.
        with self.db.session() as s:
            state = s.get(SheetSync, self.key)
            if state is None:
                state = SheetSync(key=self.key, digest=digest, row_count=len(rows))
                s.add(state)
            state.digest, state.row_count, state.synced_at = digest, len(rows), utcnow()
        return {'enabled': True, 'changed': True, 'rows': len(rows)}

    async def run(self, stop):
        if not self.settings.google_spreadsheet_id:
            return
        if not self.settings.google_credentials_file.is_file():
            logging.getLogger(__name__).warning('Google mirror not started: credentials unavailable')
            return
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.sync_once)
            except Exception as error:
                # No credential contents, HTTP response bodies or workout data in logs.
                logging.getLogger(__name__).warning('Google mirror retry: %s', type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.google_sync_interval_seconds)
            except TimeoutError:
                pass
