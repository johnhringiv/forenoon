# Email delivery setup

Forenoon delivers by emailing EPUB attachments to your Send to Kindle address.

## 1. Find your Send to Kindle address

Amazon → Account & Lists → Content Library → **Preferences** → Personal Document Settings →
_Send-to-Kindle E-Mail Settings_. Each device has an address ending in `@kindle.com`. Put it
under `users[].kindle_addresses` in `config.yaml`.

## 2. Approve the sender

On the same page, under _Approved Personal Document E-mail List_, add the exact address you
set as `smtp.sender`. Mail from any other address is not delivered. The first time, Amazon
may email the account owner asking to verify the request; confirm it within 48 hours.

## 3. Configure an SMTP relay

Any relay that lets you send from your sender address works (Resend, SES, Fastmail, ...).
With Resend: verify your domain under Domains (it gives you the SPF and DKIM records to
add), create an API key with _Sending access_ restricted to that domain, and use:

```yaml
smtp:
  host: smtp.resend.com
  port: 587
  starttls: true
  username: resend # literally "resend"; the API key is the password
  sender: digest@your-domain.example
  max_message_bytes: 38000000
```

Things to check when choosing a relay (figures as of October 2026; verify before relying on
them):

| Relay                    | Cost at one email a day                              | Message size limit          | Notes                                                                                                                        |
| ------------------------ | ---------------------------------------------------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Cloudflare Email Service | Needs Workers Paid ($5/month; 3,000 emails included) | 5 MiB including attachments | A Kindle address cannot be a "verified destination", so the free path does not apply. Set `smtp.max_message_bytes: 5000000`. |
| Amazon SES               | Fractions of a cent                                  | 40 MB after encoding        | New accounts start in a sandbox that only sends to verified addresses; request production access first.                      |
| Resend                   | Free plan: 100 emails a day, 3,000 a month           | 40 MB after encoding        | Up to 3 verified domains on the free plan; the quota is shared across them. `.epub` attachments are allowed.                 |
| Postmark                 | Free plan: 100 emails a month                        | Not confirmed               | The free plan is a hard stop at 100.                                                                                         |

If the relay's limit is below Amazon's, set `smtp.max_message_bytes` to it. A day's articles
are then split across more emails, and an article that cannot fit in one email even without
its images is skipped and reported.

The password goes in the environment as `FORENOON_SMTP_PASSWORD` (in `.env` for compose), never in
`config.yaml`. Set SPF and DKIM for the sender's domain so Amazon does not drop the mail.

## Limits

As of October 2026, Amazon accepts up to 25 attachments per email and 50 MB; sources differ
on whether 50 MB is per attachment or per email, so Forenoon treats it as per email
and stays under it (`smtp.max_message_bytes`, default 45 MB after encoding, and
`smtp.max_attachments`, default 25). Lower `max_message_bytes` if your relay's own limit is
smaller. A day's articles are packed into as few emails as these limits allow.

## Test

```sh
forenoon golden --out out        # a rendering test article
forenoon run --dry-run --out out # today's articles as files, nothing sent
forenoon run                     # real delivery
```

Delivery to the device usually takes a few minutes. If nothing arrives, check the relay's
logs first, then Amazon's Content Library → Preferences page for the approved sender.
