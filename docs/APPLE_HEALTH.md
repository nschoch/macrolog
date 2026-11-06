# Apple Health via an iOS Shortcut

FoodLogger is a web app/PWA, so it cannot call HealthKit directly. Instead it
provides a token-protected API that an iOS Shortcut fetches and writes into
Apple Health with **Log Health Sample**.

The app tracks which days the Shortcut has **confirmed** and will not offer them
for backfill again. It cannot see whether HealthKit actually saved a sample, so
the Shortcut confirms each day only after logging it.

## 1. Create a token

In FoodLogger: **Settings → Apple Health → Generate token**.

The token is shown once. It is stored only as a SHA-256 hash and can be revoked
at any time. It grants read-only access to that user's meals, weight, body fat,
and exercise, plus the ability to mark an export confirmed.

## 2. Automatic daily + backfill Shortcut

This one Shortcut catches up every unconfirmed day and can be run daily.

1. **Get Contents of URL**
   - URL: `https://your-foodlogger-host/health/pending`
   - Method: `GET`
   - Header: `Authorization` = `Bearer <token>`
2. **Repeat with Each** item in `pending`:
   - **Log Health Sample** for:
     - Dietary Energy — kcal
     - Protein — g
     - Carbohydrates — g
     - Total Fat — g
     - Fiber — g
   - Set each Health sample's **date** to `item.sample_at` (noon on that day).
     This is required for backfill; otherwise every missed day is filed under
     today.
   - Optional: `weight.kg` → Body Mass, `body_fat_pct` → Body Fat Percentage,
     and `exercise_calories` → Active Energy.
   - **Get Contents of URL** to confirm that day:
     - URL: `https://your-foodlogger-host/health/ack`
     - Method: `POST`
     - Header: `Authorization` = `Bearer <token>`
     - Request body: JSON `{"dates": [item.date]}`
3. Run manually, or add a daily Personal Automation.

The app's **Apple Health** card shows the last confirmed date, pending days, and
a warning when today was already exported or only requested but not confirmed.

## One-day endpoint

`GET /health/day` returns one day (today by default, or `?date=YYYY-MM-DD`).
It records a *request*, not a confirmation. Check its `export` object first:

```json
"export": {
  "already_requested": true,
  "already_confirmed": true,
  "confirmed_at": 1793428800
}
```

If `already_confirmed` is true, skip logging or Health gets duplicate samples.
The backfill `/health/pending` endpoint excludes confirmed days automatically.

## Response shape

`GET /health/pending` returns:

```json
{
  "last_export_date": "2026-10-30",
  "today_confirmed": false,
  "today_requested": false,
  "pending_count": 2,
  "pending": [
    {
      "date": "2026-10-31",
      "totals": {"calories": 1950, "protein": 144, "carbs": 200,
                  "fat": 54, "fiber": 27},
      "meals": [
        {"time": "12:35", "timestamp": 1793427300, "kind": "other",
         "description": "chicken rice bowl", "calories": 620,
         "protein": 45, "carbs": 70, "fat": 18, "fiber": 6, "items": []}
      ],
      "weight": {"date": "2026-10-31", "kg": 72.4},
      "body_fat_pct": 18.2,
      "exercise_calories": 0,
      "sample_at": "2026-10-31T12:00:00-07:00",
      "units": {"calories": "kcal", "protein": "g", "carbs": "g",
                "fat": "g", "fiber": "g", "weight": "kg",
                "body_fat": "%"}
    }
  ]
}
```

The endpoint caps one response at 366 days (`?limit=...`). For per-meal samples,
repeat over `meals` and use each meal's `timestamp`/`time`. The `kind` field is
legacy metadata: the app no longer asks for a meal category, so new entries
report `other`.

## Notes

- Use the `Authorization` header, **not** a `?token=` query parameter; query
  strings can end up in proxy logs.
- Each token is scoped to one user; revoke it from Settings if it leaks.
- A day is confirmed only after `/health/ack`. If the Shortcut logs to Health
  but fails before acknowledging, the day stays pending and the next run can
  create duplicate samples.
- The app cannot know whether HealthKit accepted a sample, only that the
  Shortcut said it did.
- HealthKit permissions are controlled by iOS; the first run asks for access.
- If your host uses a self-signed certificate, install/trust it on the iPhone
  first or Health Shortcuts will reject the request.
