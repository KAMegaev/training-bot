"""Deterministic numbers for dated, numbered workout notes; unknown formats fall back to AI."""
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from .importer import normalize
from .schemas import PrepareWorkoutRequest


_DATE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(.*)")
_DATE_LINE = re.compile(r"^\s*(?:\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2})(?=\s|\(|$)(.*)$")
_TRAINER_MARKER = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)")
_PROGRAM = re.compile(r"Тренировка\s+(\d+)\s*\(неделя\s+(\d+)\)", re.I)
_EXERCISE = re.compile(r"\d+\.\s+(\S.*)")
_NUM = r"-?\d+(?:[.,]\d+)?"
_WEIGHTED = re.compile(
    rf"(?P<weight>{_NUM})\s*[хx×]\s*(?:\((?P<side_reps>\d+)\s*[хx×]\s*(?P<factor>[23])\)|(?P<reps>\d+))"
    r"\s*[/хx×]\s*(?P<count>\d+)(?:\s*\((?P<note>[^()]*)\))?", re.I)
_BODYWEIGHT = re.compile(r"(?P<reps>\d+)\s*[/хx×]\s*(?P<count>\d+)(?:\s*\((?P<note>[^()]*)\))?", re.I)
_COUNT = re.compile(r"[/хx×]\s*(\d+)", re.I)


def has_trainer_marker(text):
    """A parenthesized fraction marks the whole workout only on a date line."""
    return any(_TRAINER_MARKER.search(match[1]) for line in text.splitlines()
               if (match := _DATE_LINE.fullmatch(line)))


def workout_day_in_text(text, today: date):
    """Resolve explicit numeric/Russian dates; a missing date stays missing."""
    found = []
    numeric = re.compile(
        r"(?<![\w.])(?:(?P<iso>\d{4}-\d{2}-\d{2})|"
        r"(?P<day>\d{1,2})[./](?P<month>\d{2})(?:[./](?P<year>\d{4}|\d{2}))?)"
        r"(?=$|[\s(),;:!?])")
    for match in numeric.finditer(text):
        try:
            year = int(match['year']) if match['year'] else today.year
            if year < 100:
                year += 2000
            found.append(date.fromisoformat(match['iso']) if match['iso'] else
                         date(year, int(match['month']), int(match['day'])))
        except ValueError as error:
            raise ValueError("Некорректная дата тренировки. Укажите дату в формате ДД.ММ.ГГГГ") from error
    months = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
    for match in re.finditer(r'\b(\d{1,2})\s+(' + '|'.join(months) + r')(?:\s+(\d{4}))?\b', text, re.I):
        try:
            found.append(date(int(match[3] or today.year), months.index(match[2].lower()) + 1, int(match[1])))
        except ValueError as error:
            raise ValueError("Некорректная дата тренировки") from error
    if not found:
        for word, offset in [('позавчера', 2), ('вчера', 1), ('сегодня', 0)]:
            if re.search(r'\b' + word + r'\b', text, re.I):
                found.append(today - timedelta(days=offset))
    if len(set(found)) > 1:
        raise ValueError("В сообщении несколько дат. Пришлите подходы за одну дату")
    return found[0] if found else None


@dataclass
class ParsedWorkout:
    source: str
    day: str
    gym: str
    week: int
    program_session: int
    exercises: list[dict]
    trainer: str | None = None

    def context(self):
        return {key: value for key, value in asdict(self).items() if key != "source"}

    def prepare(self, selections):
        expected = {item['index'] for item in self.exercises}
        selected = {item.exercise_index: item.exercise_code for item in selections}
        if len(selected) != len(selections) or set(selected) != expected:
            raise ValueError("Сопоставьте каждое упражнение ровно один раз, используя индексы разбора")
        groups = []
        for exercise in self.exercises:
            for group in exercise['groups']:
                groups.append(dict(group, exercise_code=selected[exercise['index']], raw_name=exercise['name']))
        return PrepareWorkoutRequest(day=self.day, gym=self.gym, week=self.week,
                                     program_session=self.program_session, trainer=self.trainer,
                                     workout_id=None, sets=groups).expand()


def parse_workout(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    date_match, program = _DATE.fullmatch(lines[0]), _PROGRAM.fullmatch(lines[1])
    if not date_match or not program:
        return None
    try:
        day = datetime.strptime(date_match[1], '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    trainer = 'да' if has_trainer_marker(lines[0]) else None
    location = _TRAINER_MARKER.sub('', date_match[2]).strip()
    if not location:
        gym = 'LikeGym'
    elif re.fullmatch(r"(?:в\s+)?(?:Орёл|Орел|Орле)|\((?:Орёл|Орел|Орле)\)", location, re.I):
        gym = 'ReZone'
    else:
        # Explicit gyms or other metadata need the conversational path; don't lose them.
        return None
    exercises, current = [], None
    for line in lines[2:]:
        title = _EXERCISE.fullmatch(line)
        if title:
            current = {'name': title[1].strip(), 'groups': []}
            exercises.append(current)
            continue
        if current is None:
            return None
        name = normalize(current['name'])
        group = dict(weight=None, reps=None, minutes=None, rpe=None, note=None, count=1)
        count = _COUNT.fullmatch(line)
        if name == 'комплекс экстензия пресс' and count:
            group['count'] = int(count[1])
        elif re.match(r"^[хx×]", line, re.I):
            continue  # Prescribed targets are not performed sets.
        else:
            bodyweight = _BODYWEIGHT.fullmatch(line) if ('подтягив' in name or 'брусь' in name) else None
            weighted = _WEIGHTED.fullmatch(line)
            if bodyweight:
                group.update(weight=0, reps=int(bodyweight['reps']), count=int(bodyweight['count']),
                             note=bodyweight['note'])
            elif weighted:
                reps = int(weighted['reps'] or weighted['side_reps'])
                notes = [weighted['note']] if weighted['note'] else []
                if weighted['factor'] == '2':
                    notes.append('на каждую руку/ногу')
                elif weighted['factor'] == '3':
                    notes.append(f'{reps}×3')
                    reps *= 3
                group.update(weight=float(weighted['weight'].replace(',', '.')), reps=reps,
                             count=int(weighted['count']), note='; '.join(notes) or None)
            else:
                return None  # Unknown result must be clarified, never silently discarded.
        current['groups'].append(group)
    exercises = [exercise for exercise in exercises if exercise['groups']]
    for index, exercise in enumerate(exercises):
        exercise['index'] = index
    return ParsedWorkout(text, day, gym, int(program[2]), int(program[1]), exercises, trainer=trainer)


def previous_workout(history):
    """Recover a structured entry while its exercise choice is being clarified."""
    for item in reversed(history):
        if item['role'] == 'assistant' and 'Проверьте запись и нажмите «Сохранить».' in item['content']:
            return None
        if item['role'] == 'user':
            parsed = parse_workout(item['content'])
            if parsed:
                return parsed
    return None
