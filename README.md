# FABLE Fair Share Explorer

A standalone Streamlit viewer for validated Scenathon results. Admins can upload a report, preview it privately, choose public views, and publish the snapshot. Published viewers do not need the app's admin token. Source workbooks and private previews are not served publicly.

## Local build and demo

Use Python 3.12 if setting up a new environment. Stop the old local Streamlit process before upgrading the generated app files. The final notebook replaces only changed app files, saves their originals next to the app folder in FABLE_App_Backups and leaves the SQLite database, reports and local secrets untouched.

From the generated FABLE_Fair_Share_Streamlit directory in Windows PowerShell:

```powershell
python -m pip install -r requirements.txt
python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:FABLE_ADMIN_TOKEN = [System.Net.NetworkCredential]::new("", (Read-Host "Paste the generated local admin token" -AsSecureString)).Password
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8514
```

Alternatively use the final notebook's local launch cell. Go to http://127.0.0.1:8514, open Admin and enter the same token. For the local demo, the app uses data/fair_share.sqlite3. A browser refresh does not restart the server. Use Ctrl+C if you started it in PowerShell, or stop_local() in the same notebook kernel that launched it.

## Commit only the app files

Use a standalone repository rooted at the generated FABLE_Fair_Share_Streamlit folder. A private repo keeps the code and release files out of an unrestricted source-code repository. The public viewer can be opened separately after review.

Commit:

```text
app.py
model.py
repository.py
views.py
publication.py
fair_share.py
fair_share_weights.json
releases.json
assets/world_map_paths.json.gz
requirements.txt
.streamlit/config.toml
.gitignore
README.md
```

Do not commit Excel reports, data/, SQLite files, .streamlit/secrets.toml, .env files, admin tokens, the PostgreSQL URL, backups, or notebook outputs. The raw geometry asset from an older build is no longer used. It can stay on your machine and does not need to be added to the repo.

For a new empty repository, create it in GitHub without a starter README, then run these commands from the app folder. Replace the remote URL with your own.

```powershell
git init -b main
git add -- app.py model.py repository.py views.py publication.py fair_share.py fair_share_weights.json releases.json assets/world_map_paths.json.gz requirements.txt .streamlit/config.toml .gitignore README.md
git diff --cached --name-only
git commit -m "Release FABLE Fair Share Explorer"
git remote add origin https://github.com/YOUR-ORG/YOUR-REPO.git
git push -u origin main
```

For an existing standalone repo, skip git init and git remote add. Verify git remote -v and git branch --show-current, stage the same explicit file list, commit and push to its intended branch. Do not force-push. If sensitive files are already tracked, stop and remove them from the branch before going live. Rotate exposed credentials if they were previously committed.

## Deploy using Streamlit Community Cloud

1. Provision a dedicated database with your team-approved managed PostgreSQL provider. Use an app role that can create tables and read/write in that database. Copy the TLS-enabled PostgreSQL URL from the provider. The app creates its tables automatically. The original workbook is archived in the private database only once per distinct file hash. Allow capacity for those reports, not just the small results. Do not use a local SQLite file for durable hosted data.
2. Open https://share.streamlit.io, connect the GitHub repo, and create an app. Select the repo, the branch you pushed, and app.py as the entry point. Choose Python 3.12 in Advanced settings.
3. Put the following in Advanced settings → Secrets, substituting your actual provider URL and a separately generated long admin token. Do not enter the placeholder strings or commit the secrets file.

```toml
FABLE_HOSTED = "true"
FABLE_DATABASE_URL = "postgresql://APP_ROLE:URL_ENCODED_PASSWORD@DB_HOST:5432/DB_NAME?sslmode=require"
FABLE_ADMIN_TOKEN = "PASTE_A_NEW_RANDOM_TOKEN_OF_AT_LEAST_24_CHARACTERS"
```

4. Deploy. First open the hosted app and sign in under Admin. Upload the approved Scenathon Excel report, validate, select the public views and publish. Local SQLite publications do not transfer through GitHub.
5. When you are ready to share, open the app's sharing settings and allow public viewers without sign-in. Test the public URL in a private/incognito window and verify that Explore and Compare work without a token while Admin still requires it. Share the hosted URL and deliver the admin token privately to the assigned admin.

- [Deploy from GitHub](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Set Streamlit secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [Manage app sharing](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)
- [Why local storage is not durable on Community Cloud](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data)

## What to show Davide

- Explore has the three-approach map, map-only presentation mode, AP/HR/EQ grouped table with Min–max, Base and Adjusted, Base/Adjusted plots and the annual trend. Current Trends allowances stay fixed. Negative signed gaps mean emissions exceed the allowance. Relative gaps divide by the absolute selected emissions and are undefined close to zero.
- Compare supports pathways at the same stage, before/after trade for the same pathway, and snapshots of the same pathway and stage. Both reports must share the benchmark. "Who crossed the allowance?" shows the gap on both sides of the zero boundary. The view appears only if both public snapshots explicitly allow it.
- For a previously published snapshot, use Admin → Snapshot history → Edit public views. To present linked map highlights, enable the linked map option on that snapshot. Views may be changed without a re-upload.
- This release is configured for Scenathon 9 with 38 participants. Register matching approved benchmark files in releases.json before admitting another event.

## Performance and checks

The viewer shares compact offline map geometry across AP, HR and EQ without changing the participant crosswalk or any scientific value. It reuses a small number of map views within each session. The uploader parses the Indicators sheet once per uploaded file and does not recalculate the preview on unrelated widget changes. Hosted database connections are pooled, and existing archived Excel bytes are not re-sent to PostgreSQL when the same workbook is republished.

If the app can't connect, inspect the hosted Manage app logs, check the database is reachable from the host, and check the URL, role and TLS settings. If the local app is stale, stop and start Streamlit rather than refreshing the browser. The local and hosted databases are separate. Regional display grouping is provisional.
