#!/bin/sh
# Builds the database sqlx checks the queries against (target/dev.sqlite3): the pipeline's
# tables plus this app's migrations. Then refreshes .sqlx, the saved query data that lets
# the crate build without a database. Run it after changing a query, a migration or
# pipeline-schema.sql. Needs sqlite3 and sqlx-cli (cargo install sqlx-cli).
set -eu
cd "$(dirname "$0")"
mkdir -p target
db="target/dev.sqlite3"
[ ! -e "$db" ] || mv "$db" "$db.old"
sqlite3 "$db" < pipeline-schema.sql
export DATABASE_URL="sqlite://$db"
cargo sqlx migrate run
cargo sqlx prepare -- --all-targets
