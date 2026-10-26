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


HEADERS = ['Дата', 'Упражнение', 'Упражнение как записано', 'Подход', 'Вес, кг', 'Повторения', 'Время, мин', 'RPE',
           'Зал', 'Тренер', 'Неделя программы', 'Тренировка программы', 'Примечание',
           'Проверить название', 'Код упражнения', 'ID тренировки', 'ID подхода']
EXERCISE_HEADERS = ['Код упражнения', 'Упражнение', 'Группа мышц', 'Оборудование']


def sheet_cell(value):
    if value is None or value == '':
        return {}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {'userEnteredValue': {'numberValue': value}}
    # Explicit stringValue prevents formula injection, including a leading '='.
    return {'userEnteredValue': {'stringValue': str(value)}}


def cell_rows(rows, *, headers=HEADERS, date_column=True):
    result = [{'values': [sheet_cell(value) for value in headers]}]
    epoch = date(1899, 12, 30)
    for row in rows:
        values = [(date.fromisoformat(row[0]) - epoch).days, *row[1:]] if date_column else row
        result.append({'values': [sheet_cell(value) for value in values]})
    return result


def update_requests(rows, sheet, *, headers=HEADERS, date_column=True):
    """Single atomic update replaces owned values, including removed trailing rows."""
    props = sheet['properties']
    sheet_id = props['sheetId']
    grid = props['gridProperties']
    row_count = max(grid['rowCount'], len(rows) + 1)
    column_count = max(grid['columnCount'], len(headers))
    requests = []
    if row_count != grid['rowCount'] or column_count != grid['columnCount']:
        requests.append({'updateSheetProperties': {'properties': {'sheetId': sheet_id,
            'gridProperties': {'rowCount': row_count, 'columnCount': column_count}},
            'fields': 'gridProperties.rowCount,gridProperties.columnCount'}})
    requests.append({'updateCells': {
        'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': row_count,
                  'startColumnIndex': 0, 'endColumnIndex': len(headers)},
        'rows': cell_rows(rows, headers=headers, date_column=date_column), 'fields': 'userEnteredValue'}})
    current_filter = copy.deepcopy(sheet.get('basicFilter', {}))
    current_filter['range'] = {'sheetId': sheet_id, 'startRowIndex': 0,
                               'endRowIndex': len(rows) + 1, 'startColumnIndex': 0,
                               'endColumnIndex': len(headers)}
    requests.append({'setBasicFilter': {'filter': current_filter}})
    # Extend number formats to new records without changing user column widths.
    if rows and date_column:
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
        self.exercise_sheet_id = settings.google_exercises_tab_id

    def sheet(self, kind='history'):
        if kind not in {'history', 'exercises'}:
            raise ValueError('Неизвестная выгрузка')
        target_id = self.sheet_id if kind == 'history' else self.exercise_sheet_id
        target_title = 'Тренировки' if kind == 'history' else 'Справочник упражнений'
        headers = HEADERS if kind == 'history' else EXERCISE_HEADERS
        response = self.session.get(self.base, params={
            'fields': 'sheets(properties(sheetId,title,gridProperties),basicFilter)'}, timeout=30)
        response.raise_for_status()
        sheet = next((s for s in response.json()['sheets']
                      if s['properties']['sheetId'] == target_id), None)
        if not sheet or sheet['properties']['title'] != target_title:
            raise ValueError(f'Ожидаемый лист «{target_title}» не найден')
        title = "'" + sheet['properties']['title'].replace("'", "''") + "'"
        last_column = chr(ord('A') + len(headers) - 1)
        header = self.session.get(self.base + '/values/' + quote(title + '!A1:' + last_column + '1', safe=''), timeout=30)
        header.raise_for_status()
        if header.json().get('values') != [headers]:
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
        journal = Journal(self.db, 'owner')
        rows = journal.history_rows()
        datasets = [('history', self.key, rows, HEADERS, True)]
        catalog = None
        if self.settings.google_exercises_tab_id is not None:
            catalog = journal.exercise_rows()
            catalog_key = 'catalog:' + self.settings.google_spreadsheet_id + ':' + str(self.settings.google_exercises_tab_id)
            datasets.append(('exercises', catalog_key, catalog, EXERCISE_HEADERS, False))
        changed = []
        with self.db.session() as s:
            for kind, key, values, headers, date_column in datasets:
                digest = hashlib.sha256(json.dumps({'headers': headers, 'rows': values},
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                state = s.get(SheetSync, key)
                if force or not state or state.digest != digest or state.synced_at <= utcnow() - timedelta(hours=6):
                    changed.append((kind, key, values, headers, date_column, digest))
        result = {'enabled': True, 'changed': bool(changed), 'rows': len(rows)}
        if catalog is not None:
            result['exercises'] = len(catalog)
        if not changed:
            return result
        transport = self.transport or GoogleSheets(self.settings)
        try:
            requests = []
            for kind, _, values, headers, date_column, _ in changed:
                sheet = transport.sheet() if kind == 'history' else transport.sheet(kind)
                requests.extend(update_requests(values, sheet, headers=headers, date_column=date_column))
            transport.update(requests)
        finally:
            if self.transport is None:
                transport.close()
        # Advance state only after Google acknowledges the entire atomic update.
        # A lost response is safe to retry: a full replacement never appends duplicates.
        with self.db.session() as s:
            for _, key, values, _, _, digest in changed:
                state = s.get(SheetSync, key)
                if state is None:
                    state = SheetSync(key=key, digest=digest, row_count=len(values))
                    s.add(state)
                state.digest, state.row_count, state.synced_at = digest, len(values), utcnow()
        return result

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
