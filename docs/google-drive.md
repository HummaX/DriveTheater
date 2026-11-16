# Google Drive connection guide

DriveTheater cannot access a private Drive folder without Google authorization. The Sources screen stores credential files only in the private runtime data directory; the JSON API never sends the secret values back.

## Public folder and API key

1. In Google Cloud Console, create/select a project and enable **Google Drive API**.
2. Create an API key. An API key identifies the project; it does not grant private-file access.
3. Share the folder and intended files using **Anyone with the link** where you are permitted to do so. An organization can prohibit public access/downloads even when a link exists.
4. In Sources choose **Public metadata API key**, enter the key and folder link/ID, then Test and Sync.
5. If Google refuses content download despite listing metadata, use service-account or OAuth access. DriveTheater does not bypass download restrictions or quota limits.

## Private folder via service account

1. Enable Google Drive API in a Google Cloud project.
2. Create a service account and, if your organization permits, a JSON key.
3. Share the intended Drive folder with the service account's `client_email` as a viewer, with download permission.
4. In Sources choose **Service account JSON**, paste the JSON, enter the folder link, Test and Sync.

A service account is not your personal Google identity. Sharing a folder is required; domain-wide delegation is not assumed. Never upload the JSON key to GitHub or paste it into issue reports.

## Private folder via OAuth browser authorization

1. Enable Drive API and configure the Google OAuth consent screen. Add your account as a test user if the project is in testing.
2. Create OAuth credentials of type **Web application**. Register the callback exactly:

   `https://YOUR_THEATER_HOST/api/sources/google/callback`

3. Google generally requires HTTPS for non-localhost web callbacks. A plain remote HTTP/IP-only install is not a valid substitute; use a domain/TLS reverse proxy, or choose service-account setup instead. Localhost may be used for local development under Google's rules.
4. Download the client JSON and enter it in Sources. Both Google's standard `{"web": {...}}` shape and a normalized client object are accepted.
5. Enter your externally reachable base URL if not configured during installation. Choose Reconnect/Authorize, sign into Google, and grant read-only Drive access.
6. Return to DriveTheater, Test access, then Sync. Refresh tokens are kept server-side; revoked/expired grants require reconnection.

OAuth apps in testing or subject to organization policies can have token lifetime/user restrictions. The application cannot remove Google's approval requirements.

## Library behavior

Sync is metadata-only and paginated. The first release lists video files directly inside the selected folder; it does not recursively crawl nested folders or resolve every Drive shortcut. Shared-drive visibility depends on your identity's granted access. Cache entries remain separate from the remote catalog so a missing remote file does not automatically erase previously downloaded media.

Thumbnails can expire or be unavailable. Hovering does not download a new source movie; scene clips and seekbar sprites appear only after that movie has been requested through Play or Download and the derivative processing has completed.
