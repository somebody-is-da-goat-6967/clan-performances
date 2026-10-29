# Clan war & ranked tracker

A GitHub Action crawls the Clash of Clans API every 3 hours, commits JSON "tabs" to `docs/data/`,
and GitHub Pages serves `docs/index.html` (tabs, search, clan filter, sortable columns, CSV export).
Every table defaults to Town Hall high to low.

Tabs: Members, Ranked, War summary, Wars, CWL summary, CWL.

## Setup
1. Create the repo, push these files.
2. Get a key at https://developer.clashofclans.com. Keys are locked to IP addresses, and GitHub
   runners have changing IPs, so route through a fixed-IP proxy (e.g. the RoyaleAPI proxy, see
   docs.royaleapi.dev), whitelist the proxy's IP on the key, and set the repo variable
   `COC_API_BASE` (Settings > Secrets and variables > Actions > Variables) to the proxy's /v1 URL.
3. Add the key as repo secret `COC_API_TOKEN`.
4. Settings > Pages > Deploy from branch > `main` / `/docs`.
5. Actions > "Crawl clans" > Run workflow.

Edit `clans.json` to change tracked clans. Wars are only recorded from the day the crawler starts
(the API shows just the current war), and a clan with a private war log cannot be read for regular wars.
History accumulates in `data/state.json`.
`docs/data/ranked_sample.json` keeps one raw ranked response so field names can be checked.

Run tests: `python3 -I tests/test_crawl.py`
