[Documentation overview](README.md)

# GitHub wiki synchronization

The [GitHub wiki](https://github.com/Karapsin/analytics_toolkit/wiki) is a generated
copy of user documentation on `main`. Edit repository docs through normal PRs;
direct wiki edits to generated pages are replaced by the next sync. Unrelated
manual pages are preserved. A name collision with a manual page stops publication,
except that the initial Home page is replaced during the first import.

The wiki includes quick start, the Airflow SQL manual, module guides, function
references, and the changelog. Developer integration and agent operations pages
stay in the repository. Links to repository files remain available from the wiki.

## Activation at release

Implementation lands on `dev` first. The workflow becomes active when an explicitly
requested release brings it to `main`; it does not publish from feature branches
or `dev`. Pushes changing docs or the exporter on `main`, published releases, and
manual runs on `main` synchronize the wiki. Every run reads the latest `main`.
A sync failure does not alter package publishing or publish partial wiki content.

Before the first release containing the workflow:

1. Open the repository's Wiki tab and save an initial page named **Home**. GitHub
   requires an initial page before the separate wiki Git repository can be cloned.
2. Create a classic personal access token for an account with repository write
   access, with `public_repo` scope for this public repository and an expiry.
3. Store it as the repository Actions secret **WIKI_SYNC_TOKEN**. Never paste
   the token into documentation, a commit, an issue, or workflow logs.

See [GitHub's wiki setup instructions](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages)
and [personal access token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

## Validation and manual retry

The exporter uses only the Python standard library and does not change package
APIs or install dependencies:

```bash
python agent_tools/wiki_sync.py --source-directory docs --check-only
python agent_tools/wiki_sync.py --source-directory docs --output-directory /tmp/wiki-preview
```

Check-only validates every included page and internal document/heading link
without writing output. Export prints `Wiki updated` or `Wiki already current`.
The output directory is a disposable preview or a separately cloned wiki, never
the repository's source documentation. Nested paths become flat page names, such
as `sql-functions-read`, retaining existing heading anchors and code examples.
Home and the sidebar provide navigation derived from repository indexes.

The `.wiki-sync-manifest.json` file tracks generated pages. Retired generated
pages are removed on the next export; other manual pages are retained. Do not
edit or remove the ownership manifest. All pages are validated before export;
a failed validation or push leaves the live wiki unchanged. Pushes never force
rewrite wiki history. If a concurrent manual edit rejects a push, rerun the job.

For a failed sync, open **Actions → wiki-sync**, inspect the error, fix missing
links, initialization, or credentials, and choose **Run workflow** on `main`.
An unchanged export creates no new wiki commit. To renew credentials, replace
**WIKI_SYNC_TOKEN** with a new token before expiry, then manually run the workflow
on `main` to verify access. The existing agent App key is not used for wiki sync.

[Documentation overview](README.md)
