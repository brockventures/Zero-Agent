---
description: Rule requiring the agent to ask for missing credentials or OAuth scopes first rather than suggesting manual UI workarounds.
globs: "*"
---

# Ask for Credentials & Access First

When a task requires credentials, API access, OAuth scopes, or permissions that are missing or insufficient:

1. **Never default to manual UI workarounds:** Do not instruct Ryan to perform manual clicks in third-party web UIs (e.g. Gmail settings, Synology DSM, Cloud dashboards) when programmatic access is achievable.
2. **Ask for access immediately:** If a link, consent prompt, API key, token, or permission is needed, generate the direct authorization URL or ask for the credential upfront.
3. **Automate upon grant:** Once credentials or approval are provided, complete the task programmatically end-to-end.
