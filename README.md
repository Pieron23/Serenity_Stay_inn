# Serenity Stay Inn Dashboard

A complete local Streamlit dashboard for a guest room business that stores data in Excel (`guest_room_data.xlsx`) and provides smart revenue, expense, and balance analytics.

## Features

- Separate revenue entry forms for `Rooms` and `Bar`
- Overall non-fixed expense entry (unexpected and bar-related costs)
- Save, update, delete, and refresh actions
- Local Excel database (auto-created on first run)
- Duplicate protection by date + revenue stream (`Rooms`/`Bar`)
- KPI dashboard with projections and break-even analysis
- Daily/weekly/monthly performance tracking
- Month-end revenue and balance forecasting
- PIN-based login home page before dashboard access
- Sensitive values masked by default with PIN-gated reveal
- Works fully offline

## Default Financial Settings

- Initial balance: `369,308 RWF`
- House rent: `590,000 RWF`
- Labor: `290,000 RWF`
- Water bill: `20,000 RWF`
- Electricity: `30,000 RWF`
- Total fixed monthly cost: `930,000 RWF`

These are stored in the `settings` sheet of the Excel file and loaded at startup.

## Excel Database Structure

The app creates `guest_room_data.xlsx` with:

### Sheet: `daily_revenue`
- `Date`
- `Revenue_Type`
- `Revenue`
- `Note`
- `Month`
- `Year`
- `Created_At`

### Sheet: `non_fixed_expenses`
- `Date`
- `Expense`
- `Category`
- `Note`
- `Month`
- `Year`
- `Created_At`

### Sheet: `settings`
- `Setting`
- `Value`

## Run Locally

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Start the dashboard:

```bash
streamlit run app.py
```

3. Open one of these URLs:
- Local machine: `http://localhost:8501`
- Same Wi-Fi/LAN: `http://<your-local-ip>:8501`

The project includes `.streamlit/config.toml` so Streamlit listens on your network (`0.0.0.0`) by default.

## Public Internet Link (Different Networks)

To share with someone outside your network, install cloudflared once:

```bash
winget install --id Cloudflare.cloudflared
```

Then open the app sidebar and click:
1. `Start public link`
2. `Refresh public link`

Copy the generated `https://...trycloudflare.com` URL and share it.

Note: `trycloudflare.com` links are temporary session links.

## Permanent Public Link (Recommended)

This project is ready for Render deployment with persistent storage:
- `render.yaml` is included
- persistent Excel path is configured with `SERENITY_DATA_DIR=/var/data`

### Steps (Render)

1. Push this project to a GitHub repository.
2. In Render dashboard, click `New` -> `Blueprint`.
3. Connect your GitHub repo and deploy using `render.yaml`.
4. Render will create a stable URL like:
   - `https://<your-service-name>.onrender.com`

Your data remains persistent because the service uses an attached disk at `/var/data`.
On first deploy, if `/var/data/guest_room_data.xlsx` is empty, the app can seed it from the bundled `guest_room_data.xlsx`.

## Files

- `app.py` - full dashboard application
- `.streamlit/config.toml` - network access configuration
- `requirements.txt` - Python dependencies
- `guest_room_data.xlsx` - local Excel database (auto-generated)

## Notes

- Revenue is one entry per date per stream (`Rooms` once/day and `Bar` once/day).
- Update/Delete actions require PIN unlock.
- Non-fixed expenses can be entered multiple times on the same date.
- Sensitive numbers are masked as `****` by default and can be revealed using the `👁 See numbers` control.
- Save/Update/Delete writes directly to the Excel database immediately, and data remains after restart.
