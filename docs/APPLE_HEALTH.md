# Apple Health via an iOS Shortcut

FoodLogger is a web app/PWA, so it cannot call HealthKit directly. Instead it
exposes a **read-only daily endpoint** that an iOS Shortcut can fetch and write
into Apple Health with the **Log Health Sample** action.

## 1. Create a token

In FoodLogger: **Settings → Apple Health → Generate token**.

The token is shown once. It is stored only as a SHA-256 hash on the server and
can be revoked at any time. It grants **read-only** access to that user's meals,
weight, and exercise.

## 2. Create the Shortcut

In the iOS Shortcuts app, create a new shortcut with these actions:

1. **Get Contents of URL**
   - URL: `https://your-foodlogger-host/health/day`
   - Method: `GET`
   - Headers: `Authorization` = `Bearer <token>`
2. **Get Dictionary Value**
   - Get `totals` from the response.
   - Then read `calories`, `protein`, `carbs`, `fat`, and `fiber` from that
     dictionary.
3. **Log Health Sample** for each value:
   - Dietary Energy — kcal
   - Protein — g
   - Carbohydrates — g
   - Total Fat — g
   - Fiber — g
4. Optional:
   - `weight.kg` → Body Mass
   - `exercise_calories` → Active Energy
5. Run it manually, or add a daily Personal Automation.

To log a specific day, append `?date=YYYY-MM-DD` to the URL.

## What the endpoint returns

```json
{
  "date": "2026-10-31",
  "totals": {"calories": 1950, "protein": 144, "carbs": 200, "fat": 54, "fiber": 27},
  "meals": [
    {"time": "12:35", "timestamp": 1793427300, "kind": "lunch",
     "description": "chicken rice bowl", "calories": 620,
     "protein": 45, "carbs": 70, "fat": 18, "fiber": 6, "items": []}
  ],
  "weight": {"date": "2026-10-31", "kg": 72.4},
  "exercise_calories": 0,
  "sample_at": "2026-10-31T12:00:00-07:00",
  "units": {"calories": "kcal", "protein": "g", "carbs": "g",
            "fat": "g", "fiber": "g", "weight": "kg"}
}
```

Daily totals are the simplest path. For per-meal samples, repeat over `meals`
and use each meal's `timestamp`/`time`.

## Notes

- Use the `Authorization` header, **not** a `?token=` query parameter; query
  strings can end up in proxy logs.
- Each token is scoped to one user; revoke it from Settings if it leaks.
- Do not run the same day's Shortcut twice unless you want duplicate Health
  samples.
- HealthKit permissions are controlled by iOS; the first run asks for access.
- If your host uses a self-signed certificate, install/trust it on the iPhone
  first or Health Shortcuts will reject the request.
