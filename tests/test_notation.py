import pytest

from training_bot.notation import parse_workout, previous_workout
from training_bot.schemas import ExerciseSelection


def entry(name, result, header='01.09.2026 (Орёл)'):
    return f'{header}\nТренировка 2 (неделя 3)\n1. {name}\nх 6 х 5\n{result}'


@pytest.mark.parametrize('name,result,weight,reps,count,note,code', [
    ('Жим', '82.5х6 /5', 82.5, 6, 5, None, 'TEST'),
    ('Жим', '82,5x6x5', 82.5, 6, 5, None, 'TEST'),
    ('Выпады', '20х(10х2) /2', 20, 10, 2, 'на каждую руку/ногу', 'TEST'),
    ('Сгибания 21', '20х(7×3) /3', 20, 21, 3, '7×3', 'TEST'),
    ('Подтягивания обратным хватом', '18х1', 0, 18, 1, None, 'TEST'),
    ('Отжимания на брусьях', '30 /1', 0, 30, 1, None, 'TEST'),
    ('Комплекс экстензия-пресс', '/3', None, None, 3, None, 'AB06'),
    ('Комплекс экстензия-пресс', 'х3', None, None, 3, None, 'AB06'),
    ('Жим', '25х6 /3 (больше тяжело закинуть)', 25, 6, 3, 'больше тяжело закинуть', 'TEST'),
    ('Жим', '0х6 /2', 0, 6, 2, None, 'TEST'),
    ('Гравитрон', '-20х6 /2', -20, 6, 2, None, 'TEST'),
])
def test_numbers_and_count_are_parsed_and_expanded_without_model(name, result, weight, reps, count, note, code):
    parsed = parse_workout(entry(name, result))
    assert parsed is not None
    assert (parsed.day, parsed.gym, parsed.week, parsed.program_session) == ('2026-09-01', 'ReZone', 3, 2)
    payload = parsed.prepare([ExerciseSelection(exercise_index=0, exercise_code=code)])
    assert len(payload.sets) == count
    assert all((item.weight, item.reps, item.note, item.raw_name) == (weight, reps, note, name)
               for item in payload.sets)


def test_decimal_values_are_not_mistaken_for_numbered_exercise_titles():
    parsed = parse_workout(entry('Жим', '82.5х6 /5\n85х4 /2', header='01.09.2026 (3/5)'))
    assert parsed.gym == 'LikeGym'
    assert len(parsed.exercises) == 1
    assert [group['weight'] for group in parsed.exercises[0]['groups']] == [82.5, 85]


def test_duplicate_numbers_and_unperformed_exercises_do_not_lose_actual_sets():
    text = entry('Комплекс экстензия-пресс', '')
    text += '\n2. Сгибания на скамье 45°\nх 8 х 4\n2. Жим\nх 6 х 5\n20х6 /2'
    parsed = parse_workout(text)
    assert [item['name'] for item in parsed.exercises] == ['Жим']
    assert parsed.exercises[0]['groups'][0]['count'] == 2


@pytest.mark.parametrize('text', [
    entry('Жим', 'непонятная запись'), entry('Жим', '20х(7х4) /3'),
    entry('Жим', '20х6 /3', header='31.02.2026'),
    entry('Жим', '20х6 /3', header='01.09.2026, зал Особый'),
    entry('Жим', '20х6 /3') + '\n' + entry('Тяга', '30х8 /2'),
    'Жим 60 на 10, три подхода',
])
def test_unknown_formats_and_multiple_dates_fall_back_without_losing_text(text):
    assert parse_workout(text) is None


def test_selection_requires_every_exercise_exactly_once():
    parsed = parse_workout(entry('Жим', '20х6 /2') + '\n2. Тяга\n30х8 /2')
    selection = ExerciseSelection(exercise_index=0, exercise_code='TEST')
    with pytest.raises(ValueError, match='ровно один раз'):
        parsed.prepare([selection])
    with pytest.raises(ValueError, match='ровно один раз'):
        parsed.prepare([selection, selection])


def test_clarification_context_stops_after_a_prepared_draft():
    history = [{'role': 'user', 'content': entry('Жим', '20х6 /2')},
               {'role': 'assistant', 'content': 'Какой вариант?'},
               {'role': 'user', 'content': 'Со штангой'}]
    assert previous_workout(history).exercises[0]['name'] == 'Жим'
    history.append({'role': 'assistant', 'content': 'Проверьте запись и нажмите «Сохранить».'})
    assert previous_workout(history) is None
