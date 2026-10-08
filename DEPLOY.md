# Hosting on a Hostinger VPS

This guide sets up a server where colleagues use DocuWare from Claude. As the
admin you add the connector once. Each colleague clicks **Connect** and types a
personal access code you give them. Nobody installs anything.

How it fits together:

```
Claude (claude.ai / Desktop)  ──HTTPS──▶  Caddy (port 443, automatic certificate)
                                              │
                                              ▼
                                     docuware-mcp (login + read-only tools)
                                              │  one read-only service login
                                              ▼
                                       DocuWare Cloud
```

- The DocuWare secret only exists in `deploy/.env` on the VPS. Colleagues never
  see it.
- Colleagues sign in with their access code once (OAuth). Their code is stored
  only as a hash (a one-way fingerprint).
- Everyone can read what the DocuWare service login can read, and nothing
  else. Nobody can change or delete anything.

## 1. DocuWare: a read-only service login

Preferred: in DocuWare, create an app registration of type "Trusted / Service".
It gives you a client ID and a client secret. Give it read access to only the
file cabinets your colleagues should see.

Fallback: a dedicated DocuWare user with read-only rights to those cabinets.

## 2. Hostinger: the VPS

1. Buy a **VPS** (not web hosting). The smallest KVM plan is enough. Pick a
   data center in the EU.
2. As the operating system, choose **Ubuntu 24.04 with Docker**.
3. In the VPS panel, open **Firewall** and allow only ports 22 (SSH), 80 and
   443.
4. In your domain's DNS, add an **A record**, for example `docuware-mcp`,
   pointing to the VPS IP address.

## 3. Install

```bash
ssh root@<vps-ip>
git clone -b <branch> https://github.com/Ksqsee/DocuwareMCP.git
cd DocuwareMCP/deploy
cp .env.example .env
chmod 600 .env
nano .env            # fill in DOMAIN, DW_MCP_PUBLIC_URL, DW_URL, DW_CLIENT_ID/SECRET
docker compose up -d --build
```

The repository is private, so `git clone` asks for a GitHub login. Use a
fine-grained personal access token with read-only access to this one repo.

Check that it works: open `https://<your-domain>/.well-known/oauth-authorization-server`
in a browser. You should see JSON, served over a valid HTTPS connection.

## 4. Give colleagues access

```bash
cd ~/DocuwareMCP/deploy
docker compose exec mcp docuware-mcp user add "Anna Muster"
```

This prints the access code once. Send it to Anna privately, for example by
phone or Teams chat, not by email to a shared mailbox.

- List users: `docuware-mcp user list`
- Someone leaves: `docuware-mcp user remove "Anna Muster"`. Their access stops
  immediately.
- Lost code: run `user add` with the same name again. The old code stops
  working.

(Run each command with `docker compose exec mcp ...` as above.)

## 5. Claude

- **You, as admin** (Claude Team/Enterprise): in the organization's settings,
  go to **Connectors**, add a custom connector, and enter the URL
  `https://<your-domain>/mcp`.
- **Colleagues:** **Settings → Connectors → DocuWare → Connect**, then type the
  access code and click **Sign in**.

## Maintenance

- **Updates:** `cd ~/DocuwareMCP && git pull && cd deploy && docker compose up -d --build`
- **Logs:** `docker compose logs -f mcp`. Each sign-in is logged with the
  person's name.
- **Backup:** the Docker volume `deploy_mcp-data` holds the user list. If you
  lose it, you have to hand out new codes; nothing else is lost.
- **Security updates for the VPS itself:** `apt update && apt upgrade` now and
  then, or turn on `unattended-upgrades`.

## Good to know

- Only Claude's own callback addresses can complete a sign-in. A look-alike
  app cannot trick someone into giving it a session.
- Sign-ins last 30 days, then Claude asks for the code again.
- Document text that Claude reads is sent to Anthropic. That's fine for
  non-sensitive cabinets. Keep sensitive cabinets out of the service login's
  rights.
