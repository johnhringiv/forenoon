FROM rust:1.92-slim-bookworm AS build
WORKDIR /src
# The queries are checked against the saved data in .sqlx, so no database is needed here.
ENV SQLX_OFFLINE=true
# Dependencies first, against an empty crate, so they are rebuilt only when they change.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && touch src/lib.rs \
    && cargo build --release --locked && rm -rf src
COPY . .
RUN touch src/main.rs src/lib.rs && cargo build --release --locked

# No shell and no package manager: the image is the binary and the C library it links to.
FROM gcr.io/distroless/cc-debian12
COPY --from=build /src/target/release/forenoon-web /usr/local/bin/forenoon-web
ENV FORENOON_DB=/data/forenoon.sqlite3
WORKDIR /data
# Fixed UID/GID 1000, the same as the app image: both write the database in the data folder.
USER 1000:1000
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["forenoon-web", "health"]
ENTRYPOINT ["forenoon-web"]
CMD ["serve"]
