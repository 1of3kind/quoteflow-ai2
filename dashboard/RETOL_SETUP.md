# E-ZFlow Retool Dashboard

## What This Is

A visual admin dashboard for contractors to manage their E-ZFlow business — quotes, appointments, material pickups, revenue, and contractor accounts.

## Screenshots

### Overview Tab
- KPI cards: Quotes today, conversion rate, revenue, pending pickups
- Revenue trend line chart
- Quote breakdown by trade (pie chart)

### Quotes Tab
- Filterable table of all quotes
- Status, payment, materials, appointment columns
- Row actions: View, Accept, Reject

### Appointments Tab
- Calendar view of scheduled jobs
- Daily schedule table with material pickup status
- Time slots, duration, store locations

### Materials Tab
- Pending pickup checklist
- Store addresses, pickup times, item counts
- Confirm pickup action

### Contractors Tab (Admin)
- All contractor accounts
- Plan status, trial info, usage, MRR

## Setup Instructions

### 1. Create Retool Account
- Go to **retool.com**
- Sign up (free tier available)

### 2. Create New App
- Click **Create new** → **App**
- Name: `E-ZFlow Dashboard`

### 3. Connect Your API
- Go to **Resources** (left sidebar)
- Click **Create new** → **REST API**
- Name: `E-ZFlow API`
- Base URL: `https://your-render-url.com/dashboard`
- Headers:
  - `Authorization`: `Bearer your-api-key`
  - `Content-Type`: `application/json`

### 4. Import Components

Since Retool doesn't support direct JSON import of full apps, build each section manually:

#### Overview Tab
1. Add **Container** → Name it "Overview"
2. Add **4 Statistic** components:
   - Title: "Quotes Today", Query: `GET /metrics`, Value: `{{getMetrics.data.total_quotes_today}}`
   - Title: "Conversion Rate", Value: `{{getMetrics.data.conversion_rate}}%`
   - Title: "Revenue This Month", Value: `${{getMetrics.data.revenue_this_month}}`
   - Title: "Pending Pickups", Value: `{{getMetrics.data.pending_pickups}}`
3. Add **Chart** (Line):
   - Query: `GET /revenue?period=daily&days=30`
   - X-axis: `date`, Y-axis: `revenue`
4. Add **Chart** (Pie):
   - Query: `GET /trades/breakdown`
   - Labels: `trade`, Values: `quote_count`

#### Quotes Tab
1. Add **Container** → Name it "Quotes"
2. Add **2 Select** dropdowns:
   - "Status Filter": Options: all, pending, accepted, rejected, expired
   - "Trade Filter": Options: all, landscaping, roofing, plumbing, autobody, electrical
3. Add **Table**:
   - Query: `GET /quotes?status={{statusFilter.value}}&trade={{tradeFilter.value}}`
   - Columns: Quote #, Customer, Trade, Total, Status, Payment, Materials, Scheduled, Date
   - Enable row actions: View, Accept, Reject

#### Appointments Tab
1. Add **Container** → Name it "Appointments"
2. Add **Date Picker**:
   - Default: Today
3. Add **Calendar** component:
   - Query: `GET /appointments?date={{datePicker.value}}`
   - Event title: `{{item.customer_name}} - {{item.trade}}`
4. Add **Table** below calendar:
   - Same query
   - Columns: Time, Customer, Trade, Hours, Store, Picked Up, Value, Paid

#### Materials Tab
1. Add **Container** → Name it "Materials"
2. Add **Alert** banner:
   - Message: `{{getPickups.data.length}} material orders ready for pickup`
3. Add **Table**:
   - Query: `GET /materials/pickups`
   - Columns: Customer, Trade, Job Date, Store, Address, Pickup By, Items, Total, Confirmed
   - Row action: "Confirm Pickup"

#### Contractors Tab (Admin Only)
1. Add **Container** → Name it "Contractors"
2. Add **Table**:
   - Query: `GET /contractors`
   - Columns: Business, Email, Phone, Plan, Active, Trial, Trial Ends, Quotes Used, Limit, MRR

### 5. Set Up Queries

For each API call, create a Retool query:

| Query Name | Method | Endpoint | Trigger |
|-----------|--------|----------|---------|
| `getMetrics` | GET | `/metrics` | On page load |
| `getQuotes` | GET | `/quotes` | On filter change |
| `getAppointments` | GET | `/appointments` | On date change |
| `getPickups` | GET | `/materials/pickups` | On page load |
| `getRevenue` | GET | `/revenue?period=daily` | On page load |
| `getContractors` | GET | `/contractors` | On page load |
| `updateQuoteStatus` | POST | `/quotes/{{selectedQuote}}/status` | On action |

### 6. Authentication

Add a login screen:
1. Create a **Modal** with **Text Input** (API key) and **Button**
2. On button click: `localStorage.setItem('api_key', apiKeyInput.value)`
3. All queries use: `Authorization: Bearer {{localStorage.getItem('api_key')}}`

### 7. Deploy

- Click **Share** (top right)
- Toggle **Public access** or invite specific users
- Copy the public URL
- Contractors access their dashboard at this URL

## Pro Tips

| Tip | How |
|-----|-----|
| Mobile-friendly | Retool apps work on mobile browsers — contractors can check pickups from their truck |
| Notifications | Add a **Timer** component that refreshes `getPickups` every 5 minutes |
| Export data | Add **Button** → `utils.exportData(getQuotes.data, 'quotes.csv')` |
| Print schedule | Add **Button** → `utils.openUrl(getDailySchedulePDF.data.url)` |

## Customization

Change colors to match your brand:
- **Settings** → **Theme**
- Primary color: `#2563eb` (E-ZFlow blue)
- Background: `#f8fafc`
- Text: `#1e293b`

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Unauthorized" error | Check API key in localStorage, verify `FEEDBACK_API_KEY` env var |
| Empty tables | Ensure your backend has data — send a test quote first |
| Charts not loading | Check that `/revenue` returns array with `date` and `revenue` fields |
| Slow loading | Add pagination to `/quotes` with `limit` and `offset` params |
