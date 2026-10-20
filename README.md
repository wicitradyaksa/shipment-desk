# Shipment Desk

A shared internal shipment, dispatch and billing workflow, currently hosted locally. Requires Python 3.10 or later and internal staff accounts. Invoice PDF export uses the local PDF libraries in `.deps`.

## Shared staff workspace and host preparation

### Staff ownership, KPIs and follow-ups

In **My work**, use **Manage task** to claim shared work, record a waiting status, or schedule a follow-up. Admin can reassign tasks and set deadlines with a reason. A task cannot be marked complete by hand: its source workflow must move on. Claims and changes are version checked so two staff cannot claim the same task. Sales task reassignment also transfers the underlying opportunity and its access; Operations task assignment is separate from the dispatch owner. If an owner is disabled or loses the relevant role, the task returns to the shared queue. Viewers cannot change tasks or view performance details.

Admin sets working weekdays, start/end hours, fixed UTC offset, task deadline hours by role, and on-time percentage targets in **KPI targets & working hours**. Numerical targets start unconfigured. The initial calendar is Monday–Friday, 09:00–18:00, UTC+480 minutes (Taiwan). Changes apply to newly opened tasks; existing tasks retain their calendar and deadline. Holidays and daylight-saving changes are not automatic. Admin can adjust an individual deadline, with the old value and reason retained in task history.

The initial dashboard measures workflow-step performance over the last 30 days: resolved steps, on-time resolution against each task's current approved deadline, average working processing hours excluding recorded waits, and recorded waiting hours. Admin also sees staff open workload, overdue tasks and resolved counts. Staff see their own resolved-task results. These are task KPIs, not sales win-rate or contractual delivery metrics. Timers begin when a step is first observed by the app; existing historical work is not backfilled. Resolving a step means the source no longer requires that action, including a change in workflow stage. Attribution uses the final task owner; it is not a measure of each person's share of a collaborative task.

Waiting statuses are **Waiting for client**, **Waiting for carrier** and **Waiting internally**. Each requires a note and a future follow-up time. Recorded waits are separated from processing hours but do not automatically extend deadlines or suppress overdue flags. Due follow-ups appear in the same work queue, with no duplicate reminder records. Completing the source step removes its reminder; reopened steps start a new task generation. Proposal follow-ups, pickup/dispatch actions, missing delivery proof and overdue invoice collection use the existing source queues. Reminders remain inside the app; email, SMS and device notifications are not sent. Background reconciliation runs every 30 seconds while the main app is running.

### Reusable customer and carrier profiles

Use **Customers & carriers** to save contacts, addresses, tax IDs, default pickup/delivery locations and instructions. Admin can edit both types, Sales edits customers, and Operations edits carriers. All three roles can read the shared address book; Viewers cannot. Duplicate normalized names within a type are blocked. Archive/restore requires a reason and preserves history.

Quote and new-shipment forms can copy customer details; dispatch can copy carrier details; draft invoices can copy a customer's billing address and tax ID. Select a profile and explicitly confirm replacing the matching fields, then review and save the form. No form is submitted automatically. These are copies: changing a profile does not rewrite saved shipments, proposals or issued invoices. Profiles contain no automatic rate or payment instruction changes.

Staff now land on **My work** after signing in (Viewers start on Shipments). This combines the next steps across opportunities, dispatch, delivery evidence, final costs, extra-charge decisions, customer invoices, receipts and supplier bills. Actions are derived from current records and disappear when their source step is complete; they cannot be marked complete without doing that work. Existing Sales/Operations notifications can be opened or marked read here. The page refreshes every 30 seconds while open and reports connection failures. It does not send email or device notifications.

**Mine and shared work** shows personally assigned opportunities and shipments plus unassigned team actions. **All work I can access** includes colleagues' work permitted by the existing role. Sales still sees only its own opportunities and financial follow-ups; Operations does not see the Admin collection queue. Assign shipment responsibility in Dispatch. Custom quote reviews and Admin approvals are shared team queues; they are not individually claimed tasks. Records retain their existing concurrent-edit protection and audit trails.

This computer is prepared as the initial host. **Start Shared Host.cmd** reads `host_config.json`; its default remains local-only. Do not start both launchers at the same time. `python run_host.py --check` checks startup configuration without opening a listener. It accepts one specific private IPv4 address (including private-network addresses in 100.64.0.0/10), requires an HTTPS certificate for non-local access and rejects public or all-interface binding. It does not install a VPN, change firewall rules, open router ports, or verify trust on staff devices.

To connect office and remote staff, the remaining setup is:

1. Choose and connect a private company network to this host and staff devices. Permit only authorized staff devices to reach the app's host and port.
2. Obtain a trusted HTTPS certificate covering the host address used by staff. Set `host`, `port`, `certificate` and `private_key` in `host_config.json`; certificate paths can be relative to this folder or absolute. Keep private keys on the host.
3. Stop the local server, use **Start Shared Host.cmd**, then check login and role access from one office device and one remote device before adding everyone. Keep the first Admin account and database on this host; do not copy the app database to staff computers.
4. Keep the computer powered and awake. Automatic start after Windows reboot and monitoring outside the app are not yet configured. The app must be running for staff access, delay monitoring and backups.

The app is not remotely accessible yet. Network membership, trusted certificates and staff-device connectivity remain unconfigured. Carrier tracking, proposal sending, client approval capture, bank verification and actual payments still require staff input or future service integrations; the work queue does not perform those external actions.

### Automatic local backups

The main app checks on startup and every five minutes whether a backup is due, and creates a consistent SQLite snapshot when the latest snapshot is at least 24 hours old. **Back Up Shipment Desk.cmd** makes an additional snapshot at any time while the app is running. Snapshots include staff accounts, session records, documents, quotes and financial history; treat them as confidential. Integrity is checked before each snapshot receives its final filename. Admin can see the latest backup time or backup failure under **My work → Host and backups**.

Snapshots are stored in `backups/` on this computer and are not automatically deleted. Monitor disk space and copy verified snapshots to separate protected storage: same-computer backups do not protect against a failed disk. No off-site backup service is configured. To restore, stop the app, preserve the current database separately, copy the chosen snapshot to `data/shipments.sqlite3`, then restart and verify the records. Restoring also restores account/session state from that time.

## Start

Double-click **Start Shipment Desk.cmd**, then open http://127.0.0.1:8879 in a browser. Keep the server window open. Stop with Ctrl+C. Alternatively run `python app.py`; use `--port 8880` if the default port is occupied.

On a new installation, run `python -m pip install --target .deps -r requirements.txt` once before using invoice PDF export. These packages are already installed in this workspace. PDF generation embeds a subset of Windows Microsoft JhengHei (`C:/Windows/Fonts/msjh.ttc`) for English and Chinese text. On another platform, set `SHIPMENT_INVOICE_FONT` to an available Unicode TrueType font; unsupported characters are rejected instead of silently omitted.

## Use

1. Create a shipment with a unique reference, customer, route, carrier, tracking reference, and expected delivery time.
2. Booking automatically creates a customer notification preview.
3. Select a shipment and update its status, delivery time, or internal operations note. Status and delivery-time changes create notification previews. Repeating the same update does not duplicate them. Notes stay internal.
4. Undelivered shipments past their expected delivery time automatically open an operations alert and create one delay notification preview. Exception status also opens an alert. Alerts resolve automatically when the triggering condition clears (delivery or revised future ETA for overdue alerts; leaving Exception for exception alerts). A later new delay can open a new alert and preview.
5. Export records to JSON for inspection or future migration. This export does not include an in-app import feature.

The empty board offers clearly labeled sample records. These remain in the database if loaded; use real records instead if you do not want sample data in your workspace.

## Data and limits

- Data is stored in `data/shipments.sqlite3` next to the app and survives restarts. For a restorable backup, stop the app and copy this file. Restore it to the same location while the app is stopped.
- The app listens only on this computer, at 127.0.0.1. Staff must sign in. Passwords are salted and hashed, sessions expire after eight hours, and roles are enforced on the server. Local HTTP is intended for this computer only.
- Carrier updates are entered manually. No carrier, email, SMS, accounting, or TMS connection is configured. All notifications are saved previews, never sent.
- The server checks delays every 30 seconds while running, even when the browser is closed. The board refreshes every 15 seconds. No checks run while the server is stopped or the computer is asleep; overdue shipments are evaluated when opened again.
- Dates are entered and displayed in the browser's local timezone and stored in UTC.
- Shipment creation and update actions use database transactions; history, previews, and alerts are saved with the shipment change.

## Expansion points

`app.py` contains the SQLite storage and local JSON endpoints. The workflow modules implement sales, dispatch, billing readiness, invoices and payment tracking; `public/` contains the interface. Carrier integrations can feed `/api/update` through a future authenticated adapter. Replace the preview writer with an outbox and delivery worker when real messaging is authorized. External bank reconciliation and accounting integrations can be added later; internal supplier approval and payment records are already available.

## Verification

Run `python -m unittest discover -s tests -v` for isolated workflow tests. Tests use a temporary database and do not alter real records.

## Bulk order intake

**Order intake** is available to Admin and Sales. Operations and Viewer accounts receive 403 from the intake API. Download the CSV template, enter orders, then use **Validate & preview prices**. Files must be UTF-8 CSV (up to 100 KB and 100 orders). Required columns are order_reference, customer, email, description, origin, destination, cargo_id, weight_kg, volume_cbm, packages, pickup and eta. Optional template columns cover route_id, requirements, return/outbound distances, charter days, tolls, fuel and other costs. Blank optional numeric cells default to zero. Use configured cargo/route IDs displayed on the page; pickup/delivery dates require a timezone and pickup must remain in the future.

Preview uses existing pricing rules. Generic orders show their proposal price; custom cargo/routes and extra requirements go to the existing Operations costing review. Fix every row error before confirming **Create Sales opportunities**. The batch is atomic, and the server rechecks prices, dates and duplicate history before saving. A stale preview requires another preview. Sending proposals, recording acceptance and creating shipments continue through the existing Sales workflow.

Customer name plus order reference is the duplicate key across staff, normalized for case and repeated whitespace. Keep customer naming consistent; aliases are separate names. Previously imported orders are skipped, while duplicates inside a file must be corrected. Retrying the same file under the same account returns its original result, even if rates have since changed. A new order needs a new reference. Intake records and source references are retained in the SQLite backup and quote decision history. Import does not automatically accept orders, send messages, confirm deals or create shipments. Browser validation reads the selected file locally before submitting it to this app; no external service receives the CSV.

## Reports

**Reports** is Admin-only. Its JSON endpoint (`GET /api/reports`) and CSV endpoint (`GET /api/reports/export?table=...`) return 403 for signed-in Sales, Operations and Viewer accounts; unauthenticated requests return 401. The report connection is query-only and all tables are calculated on one read snapshot. Reports do not create tasks, evaluate alerts, modify financial records or change workflow behavior.

Use inclusive **From**, **To** and **Currency** filters, then **Apply filters**. Blank dates include all history. **Refresh reports** reloads the applied filters; **All dates & currencies** resets them. Currency totals are always separate, with no exchange conversion. Every table has a CSV export using the applied filters. CSV uses UTF-8 with a BOM, proper quoting and formula protection for customer/carrier text. Missing measurements export as blank cells and display as an em dash.

| Report | Basis and date-range meaning |
| --- | --- |
| Profit by customer, directional route/lane, carrier and month | Shipment creation date (UTC); the month is the UTC creation month. Revenue is accepted base revenue plus approved extras, excluding invoice tax and reduced by issued credit-note net amounts. Actual costs use the billing record. Only current billing approvals enter the Approved section. All other shipment costs and any known accepted revenue appear separately as **Provisional**. Archived evidence or a changed approval fingerprint moves the shipment to Provisional. Missing commercial terms show Unconfigured currency and unknown revenue/profit. Margin is grouped profit ÷ grouped revenue; zero revenue has no percentage. |
| Receivables aging | Issued invoices selected by invoice document date; remaining balances and age are calculated as of the host's current local calendar date, using the same calculations as Payments. All active receipts reduce the balance regardless of the date filter. Clear date filters to reconcile the entire Payments page. Customer names are grouped exactly as stored on issued invoices. |
| Payables aging | Supplier bills selected by bill date; only current matching Admin approvals enter the report. All active supplier payments reduce the balance. Draft, returned, cancelled and outdated approvals are excluded. Supplier names are grouped as stored. |
| Recorded net cash movement | Actual payment/receipt date within the selected range. All reversed entries are excluded, even if reversed after that range. Customer receipts minus recorded supplier payments and customer refunds, separately by currency. This is recorded movement without an opening bank balance, other company expenditure or an as-of historical reversal reconstruction. |
| Operations KPIs | Shipments selected by creation date, grouped by recorded commercial currency (or invoice currency when needed). On-time delivery compares Dispatch's recorded `delivered_at` with the current ETA for currently delivered shipments. Delivered records without timestamps are listed and excluded from the percentage. |

Aging bands are **Current**, **1–30**, **31–60**, **61–90**, and **90+** days past due; an invoice/bill due today is Current. Only positive outstanding balances appear. Historical reports still show today's balances rather than reconstructing balances as of the filter's end date.

Exception and delay counts include saved alerts (including resolved alerts), current exception status, current overdue undelivered shipments and recorded late deliveries. Multiple reasons count once per shipment in the combined number. The app does not reconstruct original promised ETAs or infer dates from free-text history. Delivery-to-invoice days use actual recorded delivery and invoice issuance timestamps, in elapsed days. Invoice-to-fully-paid days use invoice document date and the earliest payment date on which the remaining non-reversed receipts reach the invoice total. Partial/unpaid invoices, zero-total invoices, missing dates and negative durations are excluded from those averages. Sample counts are shown so missing data cannot appear as zero-day performance.

`python tests/preview_workflow.py --reports` starts the disposable QA workspace at http://127.0.0.1:8881 with fictional TWD/USD shipments, approved and provisional costs, partial and full receipts, reversed customer and supplier payments, current/overdue balances, an exception, a late delivery and a missing delivery timestamp. The dataset and demo PDFs are temporary and never populate the real workspace. Test accounts are Admin, Sales, Operations and Viewer; their test-only credentials remain in the preview script. Run `python -m unittest discover -s tests -p test_reports.py -v` for report reconciliation, aging boundary, CSV, permission and read-only checks.


## Internal staff access

Open the app on the server computer and create the first admin account. There are no default credentials. Then use **Staff accounts** to add Admin, Operations, or Viewer accounts. Admins manage staff and shipments; Operations staff manage shipments; Viewers read and export shipment records. All staff see the same shipments. Staff can change their password. Disabling an account or changing its role immediately invalidates its sessions. You cannot disable or change the role of your own admin account.

Shipment history records the acting staff name and username. Earlier records are labeled System / legacy. Concurrent edits use shipment versions: stale saves are rejected with a reload option instead of overwriting a colleague's work. Existing shipment records are preserved automatically when upgrading.

### Other office computers

The default launcher stays on this computer. For office access, your IT administrator should assign this computer a fixed office IP and provide an HTTPS certificate trusted by staff devices, with that IP included in the certificate. Start with:

```text
python app.py --host OFFICE_IP --port 8879 --cert certificate.pem --key private-key.pem
```

Replace OFFICE_IP and certificate paths with real values. Open `https://OFFICE_IP:8879` from office devices. If Windows Firewall needs a rule, limit it to the private office network. This app refuses non-loopback startup without a certificate and key. Do not expose it directly to the public internet. Network access and certificates have not been configured by this build.

Create the first admin locally before switching to office-network mode. The server computer must stay running. The database remains on that server; staff browsers do not keep separate copies. Back up the database while the app is stopped. This is a small-team internal version, not a high-availability production TMS; account recovery currently requires local administrator maintenance.


## Sales and Operations workflow

Admins now have a **Pricing configuration** page and can assign the **Sales** role under Staff accounts. Sales users can manage their own opportunities and view shipments, but cannot edit shipment operations or approve quotes. Operations users can review all opportunities, approve custom costing and update shipments, but cannot record a client's acceptance. Admins can perform both workflows. Viewers do not have access to quote pricing.

### Configure pricing first (Admin)

Choose currency, gross margin, proposal validity and customer-facing terms (including your policy on taxes, insurance and inclusions). Add cargo types, cargo weight/volume/handling rates, and standard routes with base costs. Set maximum weight and volume for both cargo and route eligibility. Configure per-kilometre and truck-charter-per-day costs for custom estimates. Rates are not supplied or inferred from market data. All monetary values use the configured currency; there is no currency conversion or separate tax calculation. Proposals snapshot the terms and rates that applied when created or revised.

Standard cost = route base + weight × cargo cost per kg + volume × cargo cost per cubic metre + handling.

Custom estimate = outbound km × distance rate + return km × distance rate + charter days × charter rate + cargo costs + tolls + fuel surcharge + other costs. If a selected standard route needs review but has no distance or charter inputs, its base cost is used instead. Distance and charter charges are additive; Operations must remove double counting and add any missing costs before approving. Operations can edit cost lines and add extra line items. A zero estimate can enter review but cannot be approved for a client proposal.

Selling price = cost / (1 − gross margin / 100), rounded to two decimal places. This is gross margin, not a markup on cost.

### Standard shipment

1. Sales selects **Sales & Operations → Potential shipment**, enters client details, cargo description, weight, volume, packages, route, pickup and expected delivery.
2. Generic cargo on an exact configured directional route, within both sets of limits and without special requirements/additional costs, produces **Proposal ready** automatically.
3. Sales opens the client proposal, copies its text or prints/saves it as PDF, then forwards it outside the app. The client version excludes internal costs, margin and review notes.
4. Sales records when/how it was forwarded, moving the quote to **Awaiting client**.
5. After actual client approval, Sales enters an acceptance reference (contact/date/email/PO) and confirms the deal.
6. The app creates one linked shipment and an Operations inbox notification. Operations opens it and assigns the carrier and tracking reference.

### Custom cargo, route or requirements

Sales uses the same form, including outbound/return kilometres, charter days and extra charges where relevant. Non-generic cargo, custom routes, oversized loads or special requirements go to **Operations review**. Operations receives an in-app notification, reviews costs, adds a review note, and either approves the proposal or requests changes. Approval notifies the owning Sales user. Sales then follows the proposal/acceptance/handoff steps above.

Notifications are saved in the **Team inbox**, refreshed every 15 seconds while signed in. These are internal app notifications, not email or push messages. Marking a notification read affects only your own inbox. Notifications are historical; the linked quote shows its current stage.

Expired proposals and past pickup dates cannot be confirmed. Sales can revise unconfirmed opportunities; revisions supersede previous proposals and run pricing/approval again. Concurrent changes are rejected with a reload option. Closed deals cannot be reconfirmed or revised into duplicate shipments. All decisions record the acting user and time. The quote retains full cargo specifications and is available to Operations from the workflow queue; the linked shipment handles carrier assignment and delivery tracking.

The Export records button exports shipments and tracking records, as before. To back up quotes, pricing configuration, accounts and inboxes too, stop the app and back up the SQLite database.

### Dispatch and shipment documents

Open **Dispatch** after a deal closes. The shipment appears as **New handoff**, with the proposal reference, cargo specifications and requested pickup time.

1. Operations or Admin selects **Accept handoff & take ownership**.
2. Assign an active Operations owner, carrier, truck and driver, set a scheduled pickup, and save dispatch instructions. Complete assignments move to **Scheduled**.
3. Select **Record cargo collected** to move the shipment into transit, then **Confirm delivery** when received. These actions also add tracking history and customer message previews; nothing is sent externally.
4. Upload **Proof of delivery**. Delivered shipments without an active proof document remain visible in the missing-proof filter.

The board also filters unassigned shipments, overdue pickups and workflow stages. Use **Refresh board** for other staff's latest changes; saving an outdated plan is rejected. **Shipment history & tracking** opens the existing tracking editor, which remains available for manual status corrections and exceptions.

All internal staff may view dispatch and download active documents. Sales can upload client approvals, packing lists, instructions and other documents. Only Operations and Admin can change dispatch, upload proof of delivery, or archive/restore documents. Archiving requires a reason and is reversible. Uploads support PDF, PNG, JPEG and UTF-8 TXT, up to 10 MB per file. Duplicate active content in the same category is reused. File-type checks are basic validation, not antivirus scanning.

Documents are stored inside the local SQLite database with their uploader, time and checksum. Stop the app before copying `data/shipments.sqlite3` to back up accounts, quotes, dispatch plans and file contents together. **Export records** remains a shipment/tracking JSON export and does not include document contents or dispatch plans. Local disk access is outside the app's staff permissions; the database is not encrypted by the app.

### Actual costs, profit and billing readiness

The **Costs & billing** page is available to Admin and Operations for all shipments, and to Sales for shipments from their own closed opportunities. Viewers cannot access financial records.

1. Upload supplier evidence in **Dispatch** (use the Other category for invoices or receipts).
2. Operations enters actual cost lines and links each to an active shipment document. Save the draft, then confirm and submit all final costs. A shipment with no actual costs requires an explanatory note.
3. Operations proposes any extra client charges. The owning Sales user (or Admin) records client approval with a reference, or marks the charge **Not billed** with a reason. Replacing the extra-charge list resets all its decisions.
4. Review accepted base revenue, approved extras, quoted cost, actual cost, cost variance, shipment profit and margin. Pending and non-billable extras are excluded from revenue; costs remain independently recorded. Draft cost totals are provisional. The app uses the shipment's currency without exchange-rate conversion or additional tax calculation.
5. Admin may approve billing only after delivery, active proof of delivery, client acceptance, submitted costs with supporting documents, and resolution of extra charges. Concurrent edits are rejected. Changes to costs, terms, extra decisions, delivery status or the active document set cause an existing approval to be rechecked against its saved review snapshot. An outdated approval requires Admin review again.

Accepted quote prices remain fixed. For manually created shipments, Admin must enter the accepted price, original quoted cost, currency and client acceptance reference. The app does not invent those values. Currency cannot change while cost or extra-charge lines exist.

The billing queue records **internal readiness and approval**. The Invoices page handles invoice preparation and issuance; Payments handles recorded receipts and overdue balances. Sending remains manual. Shipment profit is revenue less recorded shipment costs; it does not account for unentered overheads or other costs. Billing actions have a separate staff audit history. Use **Refresh queue** to see other staff's updates.

Billing data and history are backed up with `data/shipments.sqlite3` while the app is stopped; they are not included in the existing shipment JSON export.

### Disposable browser test workspace

`python tests/preview_workflow.py` launches a disposable, loopback-only QA workspace on port 8881, with fictional rates and test accounts. Its temporary database is removed on shutdown and never changes the real workspace. Test credentials are only for this disposable server and are defined in that test script; never install them in a shared deployment.

Add `--billing` to seed a delivered test shipment and supporting documents for the billing walkthrough.

### Invoice settings, drafts and PDF export

The **Invoices** page is Admin-only. Configure company name, address, registration details, payment instructions, payment terms, numbering prefix and tax treatment first. Tax treatment must be explicitly selected: no tax (zero rate), tax added to approved charges, or tax included in approved charges. One configured rate applies to all lines, rounded to two decimal places. The app does not determine jurisdictional tax rules or submit statutory electronic invoices.

Choose a shipment with current billing approval and create a draft. Each shipment has at most one invoice record. The draft snapshots the accepted base charge, approved extra charges, currency and invoice settings; internal cost and profit data are excluded. Enter the customer billing address, optional registration ID, invoice date, due date and customer-facing notes, then save. Draft PDFs are marked **DRAFT — NOT ISSUED**.

Review the invoice and confirm **Issue invoice**. Issuance requires current matching billing approval and a saved billing address. It atomically assigns the next unique number, locks the invoice and stores the generated PDF in the database. Download and send the issued PDF manually. Changes to settings or shipment records cannot alter the stored issued PDF. An outdated draft must be refreshed after billing is reapproved. Refreshing adopts current charges and settings while retaining its billing address, dates and notes.

Use internal correction/follow-up notes to record issues separately. Notes do not alter issued amounts or generate a credit note. Credit notes and manually completed refunds are handled in **Credits & refunds**; SMTP sending is available under Integrations. Replacement invoices remain future work. Invoice data, history and issued PDFs are included in the SQLite backup, not in the shipment JSON export.

For disposable invoice testing, `python tests/preview_workflow.py --invoices` seeds fictional invoice settings and an approved shipment. These values never populate the real workspace. `output/pdf/qa-invoice.pdf` is a fictional example used for layout verification.

### Payments and collections

Open **Payments** as Admin, or select **Payments & reminders** from an issued invoice. Only issued invoices enter this queue; their amounts and PDFs remain unchanged by payment activity.

1. Check the bank transaction or receipt outside the app. Enter the amount in the invoice currency, actual receipt date, method, transaction/receipt reference and optional internal note. Confirm verification, then record the payment.
2. The app recalculates paid and outstanding amounts and marks the invoice Unpaid, Partially paid or Paid. Overpayments, future receipt dates and duplicate active references on the same invoice are rejected. Retries of the same request cannot create duplicate receipts, and concurrent changes require a refresh.
3. Filter outstanding or overdue invoices, payment status, or aging bands. An invoice becomes overdue on the day after its due date while a balance remains. Dates use the server computer's local calendar date, shown at the top of the page. Totals are kept separate by currency. Refresh balances to see other staff's changes or a new day's aging.
4. **Prepare reminder draft** creates unsent text with the customer, invoice number, due date, recorded receipts and remaining balance. Copy the current draft after review and send it through your usual email. Drafts become outdated when the invoice/payment version or date changes; the Copy action checks again before copying. Fully paid invoices cannot generate reminders.
5. To correct an entry, **Reverse entry**, explain why, and confirm. The original stays in the audit history, the balance is restored, and the correct payment can be entered separately. Reversal only corrects the ledger; it does not refund or move money.

There is no bank connection, automatic payment verification, foreign-exchange conversion, overpayment receipt allocation or automatic refund execution. Customer credit notes and completed refund records are managed in Credits & refunds; optional SMTP delivery is under Integrations. A bank receipt covering several invoices must be allocated manually to each invoice; the same transaction reference may be used once per invoice. Payment entries, reversals, reminder drafts and collections activity are stored in the same SQLite backup. The existing shipment JSON export does not include them.

`python tests/preview_workflow.py --payments` starts the isolated QA workspace with a fictional overdue issued invoice for payment testing.

### Supplier bills and payments

**Supplier bills** is available to Operations and Admin. Add a supplier, then enter the supplier invoice reference, total, dates and supporting document for its shipment. The currency comes from the shipment's commercial terms. Each bill covers one shipment and one uploaded document; consolidated supplier invoices across shipments are not supported yet.

Operations saves and submits bills for review. Admin approval requires the invoice total to exactly match the submitted actual cost lines linked to that document. Bills do not add costs again or change shipment profit. Duplicate active supplier/reference combinations and reuse of an active bill's document are rejected. Admin can return a submitted bill with a reason, reopen an approved bill, or cancel a bill; the history remains available.

Changes to matched costs, their submission or supporting evidence invalidate the approval. Admin must review again before further payments can be recorded. Only Admin may record an externally completed payment after confirming the bank transaction. Partial payments, outstanding and overdue balances are tracked separately by currency. Overpayments, duplicate active payment references on a bill and conflicting concurrent changes are rejected; retrying the same payment request cannot create a second entry.

Incorrect payment entries can be reversed with a reason, preserving the original and restoring the balance. A bill with active payments cannot be reopened or cancelled. These are manual ledger records: the app does not transfer money, connect to banks or refund payments. Supplier bills, payments and history are included in the local SQLite backup, not the shipment JSON export. Use **Refresh bills** to see other staff's changes.

The disposable `--invoices` QA workspace also includes a submitted cost and supplier document for testing this workflow without changing real records.



## Configure-later integrations

Open **Integrations** as Admin. All configuration, drafts, statement previews and accounting exports are Admin-only; other signed-in roles receive 403. No external accounts are required for the default local Test mode. This is optional infrastructure: existing proposal acceptance, dispatch, billing and payment approvals retain their normal workflow.

### Email outbox

Choose Proposal, Invoice PDF, Payment reminder or Shipment update preview, select a source record and **Prepare email draft**. Review the recipient, body and attachment before sending. Proposal drafts use the customer-facing proposal snapshot and exclude internal costing. Invoice drafts attach the immutable issued PDF. Reminders recalculate the outstanding balance. Changed proposal/reminder sources require a new draft; duplicate preparation of an unchanged source reuses the existing draft. Sending does not automatically mark a proposal forwarded, record client acceptance or alter payment records.

**Test** captures the message in the local outbox without opening a network connection. Return a test capture or cancelled draft to Draft when ready. **Live** requires explicit enabling, an SMTP hostname, port, username, sender address and STARTTLS or implicit TLS. Certificate verification stays enabled. Set `SHIPMENT_SMTP_PASSWORD` in the host process/service environment before starting the server; there is no password field and secrets are never stored in SQLite or returned by the API. A setting name being present does not confirm it is valid. Provider-specific OAuth/Graph/Gmail APIs are not implemented; choose a provider supporting the configured SMTP method, or add its adapter later.

Every message requires explicit review and Send, including Live mode. A successfully accepted SMTP request is recorded Sent; this is not a read receipt or proof of final mailbox delivery. The app persists Sending before contacting SMTP, preventing automatic resend after a crash. Exceptions leave Uncertain without exposing transport errors or credentials. To recover Sending/Uncertain, check the mail server using the displayed Message-ID, select the verified accepted/not-accepted result, supply evidence and confirm. Confirmed not accepted returns the message to Draft; an accepted result records Sent. No background delivery or automatic retries run.

### Signed carrier update inbox

Configure your provider/relay to POST JSON to `/api/integrations/tracking-webhook`. The contract is:

```json
{"event_id":"provider-unique-event-123","shipment_reference":"YOUR-SHIPMENT-REFERENCE","status":"in_transit","event_time":"2026-12-01T09:00:00+08:00"}
```

Enable the webhook in Admin settings and set `SHIPMENT_TRACKING_SECRET` (at least 32 characters) in the host environment. Headers are `X-Shipment-Timestamp` (Unix seconds) and `X-Shipment-Signature` (lowercase hexadecimal HMAC-SHA256 of the exact timestamp bytes, a period, and the raw JSON request bytes, keyed by the secret). Requests older/newer than five minutes, invalid signatures and disabled/unconfigured connections are rejected. Event IDs are globally unique: identical retries are reused, while changed payloads under an existing ID return 409. Use a provider-qualified event ID if several providers share this endpoint.

Map provider status codes to Booked, In transit, Out for delivery, Delivered or Exception in the Admin JSON mapping. Local **Queue test event** uses the same validation without requiring a secret or external network. Both signed and test events stay in the review inbox. **Open shipment** takes Admin to the existing tracking editor; verified changes are applied there with its existing version checks and history. **Mark reviewed** acknowledges the event only; it does not update shipment status, delivery time, documents or billing. Carrier-specific polling, GPS connections and automatic status application are not implemented. Office/remote reachability still depends on private-network and trusted HTTPS setup; no public listener is opened by this feature.

### Accounting export and bank matching

Configure revenue, receivables, payables, bank, invoice-tax and supplier-expense account codes when known. **Download transaction CSV** exports date/currency-filtered issued invoices (net and tax separately), non-reversed customer receipts, currently approved supplier bills and non-reversed supplier payments. Blank account mappings remain blank for review. It is a generic transaction mapping, not a provider-certified import or an accounting posting service. Supplier tax is not recorded separately by Shipment Desk, so supplier bill amounts are exported gross. The export does not reconstruct historical reversals, reconcile a general ledger, convert currencies or mark transactions as posted. Repeated exports may overlap; review and deduplicate before importing into your accounting system.

Bank statement preview accepts UTF-8 CSV up to 100 KB / 200 lines with exact headers `date,reference,currency,amount,direction`. Dates use YYYY-MM-DD, currency is a three-letter uppercase code, amounts are positive and direction is `in` or `out`. Matching requires date, normalized reference, amount and currency to agree with exactly one active recorded receipt/payment. Duplicate statement lines, ambiguous allocations, unmatched rows and invalid rows remain visible for manual review. The preview does not create or reverse financial entries, mark invoices paid or connect to a bank. Bank API feeds and write-back reconciliation require a later provider adapter.

`python tests/preview_workflow.py --integrations` starts the disposable two-currency QA dataset (same fictional records as `--reports`) so outbox preparation, accounting exports and statement matching can be tested without real credentials. The production workspace remains unconfigured until an Admin saves settings. There is no automatic installation or configuration of VPNs, email accounts, carrier accounts, accounting services or bank access.


## Credit notes and customer refunds

**Credits & refunds** is Admin-only and is also reachable from an invoice's Payments detail. Choose an issued invoice, enter a gross credit amount, date and reason, then preview the net/tax allocation. Confirm permanent issuance to assign a unique CN number and store an immutable credit-note PDF. The original issued invoice, its amount and PDF stay unchanged. Issued credit notes cannot be deleted, reversed or edited through the app; a mistaken issued credit requires a later debit/replacement process, which is not implemented.

Credits are limited to the original invoice total. Net and tax are allocated proportionately using the original invoice tax amount, with cumulative rounding so full credit exactly matches the invoice net/tax totals. Credit dates must fall between the invoice document date and the host's current date. This supports invoice-wide partial/full credits under the original single-rate tax treatment; it does not select individual invoice lines or determine statutory credit-note requirements.

Payments shows the original total, issued credits, adjusted total, receipts, refunds, outstanding balance and refund due. Outstanding is the positive part of adjusted total minus net receipts. Refund due is the positive part of net receipts minus adjusted total, with net receipts equal to active receipts less active refunds. Fully credited unpaid invoices show Credited; a paid invoice credited beyond its remaining balance shows Refund due. Further receipt entries cannot exceed the adjusted outstanding balance. Credits and refunds change the invoice's ledger version, invalidating saved reminder drafts and prepared reminder emails.

A refund is a manual record of an externally completed outgoing payment. Enter its actual date, amount, method and bank reference, then confirm verification. Amounts cannot exceed available customer credit; dates cannot precede the receipts/credits needed to fund the refund. The app does not transfer money. Duplicate request retries are idempotent, duplicate active bank references on the same invoice are blocked, and stale versions are rejected. Incorrect refund entries can be reversed with a reason while preserving history. Receipts that funded an active refund cannot be reversed until the refund ledger is corrected. Overpayment receipt entry, cross-invoice customer-credit allocation and actual bank refund execution remain unsupported.

Receivables aging uses the adjusted balance. Profit reports deduct credit-note net amounts from revenue while retaining recorded costs. Cash reports deduct active customer refunds on their actual refund dates; a separate refund-outflow table is exportable. Invoice-to-fully-paid KPIs exclude invoices with credit notes/refunds so credit settlement is not counted as full cash collection. Accounting CSV includes credit net/tax rows and customer refund rows; bank matching recognizes active customer refunds as outgoing transactions. Amounts remain separate by currency without exchange conversion. Admin work queues surface refund-due invoices. Audit events are retained in the invoice payment history, and all credit documents/refund ledgers are included in the SQLite backup.

`python -m unittest discover -s tests -p test_adjustments.py -v` checks immutable PDFs, cumulative tax allocation, partial/full credits, partial/refunded balances, reversals, retry/concurrency protection, roles, reports and exports. `output/pdf/qa-credit-note.pdf` is a fictional layout-verification sample created using a temporary database.

For a disposable walkthrough, `python tests/preview_workflow.py --adjustments` seeds an issued invoice, partial receipt, credit note and partial refund at port 8881. All values are fictional; no funds move and the real database is untouched.
