# Persona Engine 1.0

Persona Engine отвечает только за устойчивый стиль общения NOZZA.

Он намеренно отделён от:

- Memory 2.0, где хранятся факты о человеке;
- Voice profile, где живут скорость/тон/громкость TTS;
- Autonomy и safety policy;
- Planner / Task Engine / providers.

## Профиль

Поля v1:

| Поле | Значения | По умолчанию |
| --- | --- | --- |
| address | formal / informal | formal |
| verbosity | brief / normal / detailed | brief |
| humor | off / light / playful | light |
| initiative | quiet / balanced / active | balanced |
| relationship | professional / friendly / warm | friendly |

Дефолты сохраняют прежнее поведение NOZZA: на «вы», кратко, дружелюбно,
с лёгким ненавязчивым юмором и умеренной инициативой.

## Хранение

Используется существующая локальная таблица `preferences` с namespace:

`persona.address`, `persona.verbosity`, `persona.humor`,
`persona.initiative`, `persona.relationship`.

Отдельная таблица не создаётся. Имя владельца не дублируется в Persona:
оно остаётся в Memory 2.0 как profile-memory с provenance.

## Safety

Persona не принимает произвольные ключи. Поля вроде:

- safety;
- confirm;
- risk;
- autonomy;
- provider;

отклоняются.

Persona не может менять:

- каталог skills;
- risk;
- confirmation;
- права Planner/Task Engine;
- доступ providers;
- execution path.

Даже режим `initiative=active` означает только «предложить следующий шаг».
Выполнение всё равно проходит обычные confirmations.

## Brain

Hardcoded стиль вынесен из `_PLANNER_RULES` в динамический persona fragment.
Безопасный smalltalk (приветствие, благодарность, прощание, «кто ты») также
учитывает formal/informal. Тексты ошибок, safety и подтверждений намеренно не
перефразируются Persona Engine.

Порядок prompt:

1. неизменяемые правила модели и safety;
2. Persona presentation fragment;
3. контекст;
4. доступные skills.

Persona рассматривается как стиль, а не как инструкция расширить возможности.

## API

Agent loopback port:

- `GET /persona`
- `POST /persona {"op":"update","profile":{...}}`
- `POST /persona {"op":"reset"}`

Ответ GET содержит текущий profile, defaults, допустимые options и UI labels.

## Native UI

Control Center → Настройки → Persona NOZZA:

- обращение;
- подробность;
- юмор;
- инициативность;
- тон отношений;
- сохранить;
- вернуть по умолчанию.

## Следующий этап

Persona Engine задаёт форму общения, но не уровень самостоятельности.
Следующий отдельный слой roadmap: Autonomy Levels / Proactivity policy.
