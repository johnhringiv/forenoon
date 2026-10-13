# Recipes: publications that need Calibre

Most sources are plain feeds. A few are better read through a
[Calibre news recipe](https://manual.calibre-ebook.com/news.html): publications behind a
subscriber login, such as the Financial Times, or sites whose pages do not extract well. For
those, an optional fourth container runs Calibre once a day and Forenoon reads the result.

Use a recipe only for content you are entitled to read. You are responsible for checking
that the publication's terms allow it.

## How it fits together

1. The `forenoon-recipes` container runs each recipe at `RECIPES_TIME` (default 05:00) and
   writes one issue per day to `<data>/recipes/<folder>/<date>.epub`.
2. At delivery time Forenoon opens the newest issue of each recipe source and splits it into
   its articles. They are candidates like any others: ranked, capped, given rating links and
   never sent twice. No page is fetched for them, so an article the recipe reached with your
   login is read from the issue alone.
3. The recipes container sees only the `recipes` folder. Your logins are in its environment
   and nowhere else; the app, the feedback service and the tunnel never see them.

Set `RECIPES_TIME` early enough for the recipes to finish before `delivery_time`. A recipe
may take several minutes.

## Set it up

1. List the recipes in `<data>/recipes/recipes.conf` (start from
   `deploy/recipes.example.conf`):

   ```
   wired = Wired Daily Edition
   ft = Financial Times
   ```

   The left side is a folder name you choose. The right side is the name of a built-in
   recipe, or the file name of your own `.recipe` file placed in the same folder.

2. Add a matching source for each on the web pages' **Sources** page: tick "Calibre recipe"
   and give the folder name (`wired`, `ft`) in place of a feed address.

3. For recipes that need a login, create `recipes.env` beside your `.env` (start from
   `deploy/recipes.example.env`). For the folder `ft`:

   ```
   RECIPE_FT_USERNAME=you@example.com
   RECIPE_FT_PASSWORD=...
   ```

   Keep this file out of version control and readable only by you (`chmod 600`).

4. Turn the container on:
   - **`deploy.sh`:** set `RECIPES_IMAGE=ghcr.io/<owner>/<repo>-recipes:latest` in `.env`
     and run `deploy.sh` again. `poll-deploy.sh` then keeps it up to date.
   - **docker compose:** add `recipes` to `COMPOSE_PROFILES`.

5. Try it without waiting for the schedule:

   ```sh
   sudo docker exec forenoon-recipes python3 /usr/local/bin/runner.py once
   sudo docker exec forenoon forenoon run --dry-run
   ```

   The first prints one line per recipe. The second lists the recipe source in its report.

## When something goes wrong

- `sudo docker logs forenoon-recipes` shows each run and the last lines of any failure.
- A source with no issue from the last `max_age_days` counts as a failed source, so the
  usual alert arrives after it has failed several runs in a row.
- Recipes break when a site changes. The fix is nearly always a newer Calibre: the image is
  rebuilt monthly, and `poll-deploy.sh` replaces only the recipes container when it changes.
- To find a recipe's exact name:
  `sudo docker exec forenoon-recipes ebook-convert --list-recipes | grep -i wired`
- A recipe's login is passed to `ebook-convert` on its command line inside the recipes
  container. Nothing else runs in that container, and the login is never logged.
- Issues are kept for 7 days (`RECIPES_KEEP_DAYS`).
