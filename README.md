# loxley-reddit

Every morning, pulls new comments from Chicago-area ComEd bill threads via the
Arctic Shift archive and saves them to `data/`. A Claude skill reads them and
updates the Loxley Reddit CRM.

- Add a thread by hand: paste its URL on a new line in `urls.txt`.
- Change which subreddits are searched: edit `SUBS` at the top of `fetch.py`.
- Run now: Actions tab -> "Reddit fetch" -> Run workflow.
- If something looks off, check `data/debug/` and the `errors` in `data/index.json`.
