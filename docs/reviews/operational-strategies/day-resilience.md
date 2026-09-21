# Устойчивость плана рабочего дня

При увеличении времени пути или обслуживания исходные маршруты начинают нарушать ограничения. Перепланирование сохраняет допустимость, но часть заявок может остаться без назначения. Это проверка заданных сценариев, а не оценка вероятности успешного дня.

Reproduce offline: `.venv/bin/python scripts/check_resilience.py --time-limit 3.0 --solver hybrid`.

Morning backend: hybrid; dynamic backend: hybrid.

Solver limit: 3.0 seconds per solve/profile (preparation and fallback add overhead). Inputs and event selection are reproducible; wall-clock solver choices can vary. All source demo SHA-256 hashes are recorded in `day-resilience.json` and verified unchanged.

Fixed execution keeps the published engineer/order and never starts earlier than its appointment. Each leg and service is rounded up after scaling. Delays propagate through the route; waiting can absorb them. A later allowed window is used if reachable; otherwise the late visit remains in execution and counts as a breach. Appointment windows constrain service start, while service must finish before shift end. These are open routes: no return trip is required by the model.

Replacement plans are separate morning counterfactuals with known stress and the original issued stock. They are independently checked with `require_valid`; their pending counts are not execution breaches. Valid plans may leave work pending. These experiments are sensitivity tests, not probabilities or observed service performance.

| Region | Stress | Window breaches | Shift breaches | Distinct breached jobs | Initial pending | Max start delay | Replacement pending |
|---|---|---:|---:|---:|---:|---:|---:|
| Восток | baseline | 0 | 0 | 0 | 0 | 0 min | — |
| Восток | travel+20% | 7 | 1 | 8 | 0 | 32 min | 0 |
| Восток | travel+40% | 12 | 4 | 16 | 0 | 63 min | 3 |
| Восток | service+20% | 8 | 7 | 15 | 0 | 60 min | 4 |
| Восток | travel+40% service+20% | 21 | 9 | 27 | 0 | 111 min | 7 |
| Юго-восток | baseline | 0 | 0 | 0 | 12 | 0 min | — |
| Юго-восток | travel+20% | 7 | 2 | 9 | 12 | 62 min | 16 |
| Юго-восток | travel+40% | 11 | 4 | 14 | 12 | 130 min | 17 |
| Юго-восток | service+20% | 9 | 6 | 14 | 12 | 78 min | 16 |
| Юго-восток | travel+40% service+20% | 19 | 12 | 27 | 12 | 142 min | 23 |
| Югоцентр | baseline | 0 | 0 | 0 | 1 | 0 min | — |
| Югоцентр | travel+20% | 4 | 2 | 6 | 1 | 35 min | 2 |
| Югоцентр | travel+40% | 6 | 3 | 8 | 1 | 78 min | 3 |
| Югоцентр | service+20% | 5 | 5 | 9 | 1 | 46 min | 3 |
| Югоцентр | travel+40% service+20% | 11 | 8 | 16 | 1 | 124 min | 5 |

## Dynamic day

Each region receives two 80-minute emergencies, a future cancellation, an unavailable engineer, a working-status fact extending service by 20 minutes, an explicit completion fact, a JSON save/restore, another replan and an end-of-shift replan. Emergency anchors use existing known addresses. Travel redirection is disabled; in-progress work is preserved. The Stable candidate is approved. All three proposals at every stage are independently validated.

Checks after every approval: every catalog job is exactly in remaining work, frozen work or cancellations; no duplicate frozen jobs or rescheduling of frozen work; frozen visit records stay unchanged except the job with a new explicit fact; per-engineer consumed + reserved + available equals issued stock with no negative amounts. Save/restore must preserve the complete state and current workbench.

| Region | Event | Time | Remaining assigned | Pending | Urgent pending | Frozen |
|---|---|---:|---:|---:|---:|---:|
| Восток | emergency-1 | 09:05 | 60 | 0 | 0 | 7 |
| Восток | emergency-2 | 09:25 | 61 | 0 | 0 | 7 |
| Восток | cancel | 09:26 | 60 | 0 | 0 | 7 |
| Восток | unavailable | 09:27 | 60 | 0 | 0 | 7 |
| Восток | delay-fact-replan | 10:00 | 60 | 0 | 0 | 7 |
| Восток | completion-replan | 11:30 | 52 | 0 | 0 | 15 |
| Восток | post-restore | 11:31 | 52 | 0 | 0 | 15 |
| Восток | end-of-shift | 22:00 | 0 | 1 | 0 | 66 |
| Юго-восток | emergency-1 | 09:05 | 64 | 12 | 0 | 8 |
| Юго-восток | emergency-2 | 09:25 | 65 | 12 | 0 | 8 |
| Юго-восток | cancel | 09:26 | 64 | 12 | 0 | 8 |
| Юго-восток | unavailable | 10:30 | 64 | 12 | 0 | 8 |
| Юго-восток | delay-fact-replan | 10:31 | 64 | 12 | 0 | 8 |
| Юго-восток | completion-replan | 11:30 | 62 | 12 | 0 | 10 |
| Юго-восток | post-restore | 11:31 | 62 | 12 | 0 | 10 |
| Юго-восток | end-of-shift | 22:00 | 0 | 17 | 2 | 67 |
| Югоцентр | emergency-1 | 09:05 | 51 | 1 | 0 | 5 |
| Югоцентр | emergency-2 | 09:25 | 52 | 1 | 0 | 5 |
| Югоцентр | cancel | 09:26 | 51 | 1 | 0 | 5 |
| Югоцентр | unavailable | 09:27 | 51 | 1 | 0 | 5 |
| Югоцентр | delay-fact-replan | 10:00 | 51 | 1 | 0 | 5 |
| Югоцентр | completion-replan | 10:50 | 48 | 1 | 0 | 8 |
| Югоцентр | post-restore | 10:51 | 48 | 1 | 0 | 8 |
| Югоцентр | end-of-shift | 22:00 | 0 | 5 | 0 | 52 |

## Сравнение всех альтернатив

В JSON сохранены показатели, цели и хеши всех трёх вариантов до утверждения Stable. Разница дороги — Cost минус Stable; отрицательное число означает экономию. Реакция — минуты от поступления новой аварии до начала работ (Stable / SLA). Утренние пересчёты выше используют прежнюю цель численности, а дневной Cost — минуты дороги.

| Регион | Событие | Уникальных планов | Реакция Stable / SLA | Разница дороги, мин | Рекомендация | Расчёт, с |
|---|---|---:|---|---:|---|---:|
| Восток | emergency-1 | 2 | 570 / 238 | +0 | sla | 9.018 |
| Восток | emergency-2 | 2 | 379 / 136 | +0 | sla | 9.017 |
| Восток | cancel | 3 | — / — | -227 | stable | 9.015 |
| Восток | unavailable | 3 | — / — | -220 | stable | 9.014 |
| Восток | delay-fact-replan | 3 | — / — | -265 | stable | 9.014 |
| Восток | completion-replan | 3 | — / — | -243 | stable | 9.014 |
| Восток | post-restore | 3 | — / — | -243 | stable | 9.013 |
| Восток | end-of-shift | 1 | — / — | +0 | stable | 7.724 |
| Юго-восток | emergency-1 | 2 | 468 / 428 | +0 | sla | 9.284 |
| Юго-восток | emergency-2 | 1 | 342 / 342 | +0 | stable | 9.282 |
| Юго-восток | cancel | 3 | — / — | -103 | stable | 9.272 |
| Юго-восток | unavailable | 3 | — / — | -52 | stable | 9.261 |
| Юго-восток | delay-fact-replan | 3 | — / — | -103 | stable | 9.263 |
| Юго-восток | completion-replan | 3 | — / — | -103 | stable | 9.259 |
| Юго-восток | post-restore | 3 | — / — | -103 | stable | 9.262 |
| Юго-восток | end-of-shift | 1 | — / — | +0 | stable | 9.024 |
| Югоцентр | emergency-1 | 2 | 248 / 63 | +0 | sla | 9.017 |
| Югоцентр | emergency-2 | 2 | 317 / 72 | +0 | sla | 9.017 |
| Югоцентр | cancel | 3 | — / — | -30 | stable | 9.011 |
| Югоцентр | unavailable | 3 | — / — | -30 | stable | 9.012 |
| Югоцентр | delay-fact-replan | 1 | — / — | +0 | stable | 9.012 |
| Югоцентр | completion-replan | 2 | — / — | -45 | stable | 9.012 |
| Югоцентр | post-restore | 2 | — / — | -45 | stable | 9.012 |
| Югоцентр | end-of-shift | 1 | — / — | +0 | stable | 8.983 |

После ручного статуса инженер не возвращается автоматически к симуляции исполнения: его последующие работы требуют фактов завершения. Поэтому остаток на 22:00 включает неподтверждённые работы и не является числом потерь от стресс-событий; смотреть качество назначения нужно также на промежуточные строки. Столбец аварий относится к оставшимся заявкам, а не к доказанным фактическим пропускам выезда.

All listed assertions passed in this run. Frozen work primarily reflects the session simulation; only the deliberately injected delay/completion is a dispatcher fact. End-of-shift pending is therefore a modeled residual, not a claim that jobs were actually completed. The stress execution and dynamic event experiments are separate: dynamic events use the saved travel matrix and ordinary durations except the explicit delay fact. Cached road/public-transport estimates and approximate coordinates remain limitations of these demos; there is no live traffic, stochastic outage model, emergency SLA guarantee or optimality proof.
