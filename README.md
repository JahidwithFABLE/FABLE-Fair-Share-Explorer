# FABLE Fair Share Explorer

A Streamlit viewer for Scenathon snapshots with three fairness approaches, participant maps, a grouped min–max table, and public comparisons. Administrators preview a workbook before choosing what the app will display.

## Storage for the deployed app

This deployment keeps code on **main** and storage in **app-data** within the same GitHub repository. On the first successful publish, the app creates app-data if it is missing. Changes to main do not overwrite app-data. Do not merge the data branch into main.

Under fair-share/v1 on app-data, index.json is the catalogue and contains publication settings, runs/ holds calculated JSON, and sources/ contains the original workbooks. Each workbook is stored byte-for-byte once per SHA-256 digest. A new or changed publication is attached to the branch in one Git update. Draft previews do not write any files to GitHub.

Uploading and clicking **Publish** in a public repository also makes the workbook and calculated JSON accessible on GitHub. **Unpublish** only hides the snapshot in the Streamlit app. It does not delete any GitHub files or history. The public-view controls also apply only to the app. Use this public storage only for reports intended for public release.

GitHub-mode uploads are limited to 95 MiB per workbook. Many large workbooks will increase repository history. Local SQLite is retained for local development and PostgreSQL remains optional for other deployments. Local snapshots are not transferred to GitHub automatically.

## Deploy from GitHub

1. Push the app code to the main branch of JahidwithFABLE/FABLE-Fair-Share-Explorer, or your chosen repository. Do not manually push workbooks, database files, backups, notebooks or secrets.
2. In GitHub, create a fine-grained personal access token limited to **only the app repository**, with repository **Contents: Read and write**. The token must belong to an account that can write that repository. Do not commit the token. The Admin token is separate and grants access to Admin in the web app.
3. In Streamlit Community Cloud create the app from the GitHub repo, branch **main**, entry point **app.py**. Choose Python 3.12. In Advanced settings → Secrets add the values below, replacing both placeholders with the real tokens.

```toml
FABLE_HOSTED = "true"
FABLE_STORAGE = "github"
FABLE_GITHUB_REPOSITORY = "JahidwithFABLE/FABLE-Fair-Share-Explorer"
FABLE_GITHUB_TOKEN = "PASTE_NEW_REPOSITORY_SCOPED_PAT"
FABLE_ADMIN_TOKEN = "PASTE_PRIVATE_APP_ADMIN_TOKEN"
```

FABLE_DATABASE_URL is not used in GitHub mode. After deploy, sign into Admin and upload an approved workbook. After preview and Publish, check that the snapshot appears in Explore and the app-data branch contains the workbook, run JSON and index. Then verify Explore in a signed-out or incognito window.

## Local test

From the generated app folder in PowerShell, install requirements with python -m pip install -r requirements.txt, then use the notebook's Local launch cell. By default this uses SQLite and data/fair_share.sqlite3 and does not push anything to GitHub.

For local or hosted Admin, use a long, private token of at least 24 characters. Generate one with python -c "import secrets; print(secrets.token_urlsafe(32))", then enter the same token under Admin. Users of Explore and Compare do not need it.

## First push and later updates

App files to track: app.py, model.py, repository.py, github_storage.py, views.py, publication.py, fair_share.py, fair_share_weights.json, releases.json, assets/world_map_paths.json.gz, requirements.txt, .streamlit/config.toml, .gitignore, and README.md.

When updating the existing release for GitHub storage, stage only the four changed files:

```powershell
git add -- app.py github_storage.py requirements.txt README.md
git diff --cached --name-only
git commit -m "Store published Fair Share snapshots on GitHub"
git push origin main
```

- [Creating a fine-grained PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token)
- [Streamlit Community Cloud deployment](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Streamlit Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
