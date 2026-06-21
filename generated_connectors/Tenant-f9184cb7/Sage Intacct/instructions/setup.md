# Sage Intacct Connector — Setup

The Sage Intacct connector talks to the Intacct **XML Gateway**
(`https://api.intacct.com/ia/xml/xmlgw.phtml`). Authentication is
multi-credential and *not* OAuth — every request carries both a
"sender" pair (the Web Services partner) and a "user" pair (a real
Intacct user with API privilege).

## 1. Web Services partner (sender_id / sender_password)

The Sender ID and Sender Password are provisioned by Sage Intacct
support after you sign the Web Services agreement.

  * If you have **never used Intacct Web Services before**, file a case
    with Intacct support requesting Web Services activation. They will
    issue a Sender ID + Sender Password.
  * These credentials identify your *integration*, not your tenant — the
    same pair can serve many companies if you operate a SaaS like Shielva.

## 2. Intacct user with API privilege (user_id / user_password)

We recommend a **dedicated service user** rather than a real human.

  1. Log into Intacct as an Administrator.
  2. Go to **Company → Admin → Users → Add**.
  3. Create a user (for example `shielva_api`). Set a strong password.
  4. On the **Roles** tab, attach a role with permission on every
     Intacct object you intend to read or write through Shielva
     (Customer, Vendor, AR, AP, GL, Employee, Project, …).
  5. Go to **Company → Setup → Subscriptions → Web Services** and
     subscribe the user.

## 3. Company ID (company_id)

The Company ID is shown in **Company → Information** in the Intacct UI.
It is also called the Org ID on some screens.

## 4. Optional scoping (location_id / entity_id)

Multi-entity companies can scope requests to a single location or
entity. Leave these blank for single-entity companies.

## 5. Provide to Shielva

In Shielva, install the **Sage Intacct** connector and fill in the
install form:

  * `sender_id` — from step 1
  * `sender_password` — from step 1
  * `user_id` — from step 2
  * `user_password` — from step 2
  * `company_id` — from step 3
  * `location_id` — optional
  * `entity_id` — optional

The connector validates the full credential chain on install by running
a 1-row `readByQuery` against `GLACCOUNT`. If you see `Sign-in
information is incorrect` (Intacct error `XL03000006`), one of the five
required credentials is wrong or the user has not been subscribed to
Web Services.

## 6. Verify

After install, click **Health check** in the Shielva connector page.
You should see status `CONNECTED` and health `HEALTHY`.
