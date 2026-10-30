---
name: homelab-routing
description: Standard operating procedure for provisioning, configuring, exposing, and routing homelab service URLs across Host 1 Nginx wildcard SSL reverse proxy (with mandatory LAN ACLs) and UniFi static DNS shortnames. Enforces strict LAN-only default and prohibits public Cloudflare Tunnel/DNS exposure without explicit user authorization.
---

# Homelab Service URL & Port-Free Shortname Provisioning (The 2-Layer Private LAN Default)

Whenever asked to configure, route, or create a URL for any homelab service (e.g., `service.brock.ventures`), Zero **MUST** provision the **2-Layer Private LAN Stack** by default.

## Hard Security Invariant: Private LAN-Only by Default
- **NEVER expose a homelab service to the public internet by default.**
- **NEVER** inject an ingress rule into a Cloudflare Tunnel (`cfd_tunnel`) or create a public Cloudflare DNS record (`tools/cloudflare.py`) unless:
  1. Ryan **explicitly requests** that the specific service be accessible from the public internet outside the home LAN, **AND**
  2. The service is protected by a verified authentication gate (Cloudflare Zero Trust Access policy or strong built-in login authentication). Never expose unauthenticated dashboards, household data, or admin interfaces externally.

---

## 1. The 2-Layer Private LAN Standard (Mandatory Default)

### Layer 1: Host 1 Nginx Reverse Proxy, Wildcard SSL & Mandatory LAN ACL
- **SSL Termination:** Add a server block in `/etc/nginx/conf.d/http.brock_ventures_ssl.conf` on Host 1 (`<HOST_1_IP>`) terminating the Let's Encrypt wildcard certificate (`$NAS_DOCKER_ROOT/scripts/.acme.sh/brock.ventures_ecc/fullchain.cer`).
- **Mandatory RFC 1918 LAN Subnet Guard (`allow` / `deny`):** Because WAN ports 80/443 are forwarded on the gateway for specific public domains, every internal `.brock.ventures` server block **MUST** enforce private LAN client IP restrictions inside the `server` block to prevent external `Host:` header / SNI spoofing against the WAN IP:
  ```nginx
  allow <RFC1918_CLASS_C_CIDR>;
  allow <RFC1918_CLASS_B_CIDR>;
  allow <RFC1918_CLASS_A_CIDR>;
  allow 127.0.0.1;
  deny all;
  ```
- **HTTP-to-HTTPS & WebSockets:** Include port 80 301-redirect to `https://$host$request_uri` and proxy WebSocket headers (`Upgrade $http_upgrade`, `Connection $connection_upgrade`) with 600s timeouts.
- **Verification:** Always run `sudo nginx -t && sudo nginx -s reload` on Host 1 to verify syntax before declaring success.

### Layer 2: UniFi Static DNS & Single-Word Shortname (`service/`)
- **Controller Static DNS API:** Post static A-records via UniFi Network API (`/proxy/network/v2/api/site/default/static-dns`) using the `zero` service account and CSRF token:
  1. `service.brock.ventures` -> `<HOST_1_IP>`
  2. `service.home` -> `<HOST_1_IP>`
- **Naked Shortname Catch-All:** On UCG-Max (`<GATEWAY_IP>`), ensure `address=/service/<HOST_1_IP>` is appended to `/data/udapi-config/brock_ventures.conf` and symlinked to `/run/dnsmasq.dhcp.conf.d/` so single-word browser shortcuts (`service/`) resolve cleanly on the local LAN. Host 1's `/etc/nginx/conf.d/http.shortname_redirect.conf` automatically 302-redirects naked shortnames to `https://$shortname.brock.ventures/`.

---

## 2. Optional Layer 3: Public Internet Exposure (Explicit Opt-In Only)
- **Trigger Condition:** Execute **ONLY** when Ryan explicitly asks to expose a service to the public internet AND authentication has been verified.
- **Execution:**
  1. Confirm authentication gate (Cloudflare Access application policy or built-in auth).
  2. Add ingress rule to Cloudflare Tunnel (`2befb2df-ea17-4ae6-b13b-38011fa1a567`) or public DNS record via `tools/cloudflare.py` passing `confirm_public_exposure=True`.

---

## 3. Verification Checklist Before Reporting Done
1. **LAN HTTPS test:** `curl -IL --resolve service.brock.ventures:443:<HOST_1_IP> https://service.brock.ventures` returns HTTP 200.
2. **Local Shortname test:** `curl -IL http://service/` (or `Host: service` to `<HOST_1_IP>`) returns HTTP 302 redirecting to `https://service.brock.ventures/`.
3. **Public DNS Absence Check (for LAN-only services):** Verify no public `CNAME` or `A` record exists in Cloudflare DNS for `service.brock.ventures`.
