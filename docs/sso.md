# Sign in with SSO

Sidus can use an OpenID Connect provider for sign-in: Authentik, Microsoft Entra ID, Google, Keycloak, Duo, or any other. It still has one owner; you link your own identity at the provider, and only that identity can sign in.

![Sign-in settings](sso.png)

1. In **Settings > Sign-in**, pick the provider. Copy the **Redirect URI** Sidus shows (`https://<your-sidus>/api/auth/oidc/callback`).
2. At the provider, create an OIDC client (confidential, authorization code) with that redirect URI. Copy its issuer URL, client ID, and client secret into Sidus and click **Save**. Sidus checks `<issuer>/.well-known/openid-configuration` right away.
3. Click **Link with …** and sign in at the provider. Sidus records that identity (issuer + subject).

The sign-in page then shows **Sign in with …** next to the password form. Once that works, you can turn password sign-in off in Settings. If you lose access to the provider, `docker exec -it sidus python -m sidus reset-password` sets a new password and turns password sign-in back on.

The redirect URI is filled in from the address in your browser, and you can edit it. If it shows `localhost`, change it to the address your other devices use, for example `http://192.168.1.10:7878` or `https://sidus.your-tailnet.ts.net`; or set `SIDUS_PUBLIC_URL` in `.env` so Sidus always uses that address. Behind a reverse proxy, Sidus reads `X-Forwarded-Host` and `X-Forwarded-Proto`. Linking has to be done from the redirect address; **Sign in with …** works from any address and moves you there first. Behind HTTPS (the Tailscale sidecar or a reverse proxy), also set `SIDUS_SECURE_COOKIES=true`.

[Back to the README](../README.md)
