# syntax=docker/dockerfile:1

# ---- build ----------------------------------------------------------------
FROM node:24-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
# `nice` inside the RUN: the build runs in the Docker daemon on the live host, next to
# ingest and STT, and niceness on the `docker build` client does not reach it.
RUN nice -n 19 npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html embed.html biome.json ./
COPY src ./src
COPY public ./public

# Off unless a build explicitly asks for it. The fixtures are reached through one
# dynamic import in a branch that folds away when this is not "1", so a release
# image does not merely avoid the sample corpus — it does not contain it.
ARG VITE_MOCK=0
ENV VITE_MOCK=${VITE_MOCK}
# Blank leaves analytics off and the privacy page without a contact (docs/ANALYTICS.md).
ARG VITE_GTM_CONTAINER_ID=""
ENV VITE_GTM_CONTAINER_ID=${VITE_GTM_CONTAINER_ID}
ARG VITE_PRIVACY_CONTACT=""
ENV VITE_PRIVACY_CONTACT=${VITE_PRIVACY_CONTACT}
# The public site's identity (.env.example "public site"). A blank host: no landing page,
# canonical link or analytics anywhere, robots.txt closed, no sitemap; blank wording: generic.
ARG VITE_PUBLIC_HOST=""
ENV VITE_PUBLIC_HOST=${VITE_PUBLIC_HOST}
ARG VITE_SITE_NAME=""
ENV VITE_SITE_NAME=${VITE_SITE_NAME}
ARG VITE_SITE_DESCRIPTION=""
ENV VITE_SITE_DESCRIPTION=${VITE_SITE_DESCRIPTION}
ARG VITE_SITE_ABOUT=""
ENV VITE_SITE_ABOUT=${VITE_SITE_ABOUT}
RUN nice -n 19 npm run build

# ---- serve ----------------------------------------------------------------
FROM nginx:1.31-alpine AS runtime
# A template: the stock entrypoint renders it into conf.d/default.conf at start-up,
# substituting only the EMBED_* and PUBLIC_* variables (nginx.conf's header explains why).
COPY nginx.conf /etc/nginx/templates/default.conf.template
ENV NGINX_ENVSUBST_FILTER="^(EMBED_|PUBLIC_)"
# Who may frame /embed/* (docs/EMBED.md): a CSP `frame-ancestors` source list.
ENV EMBED_FRAME_ANCESTORS="*"
# The one hostname search engines may index (nginx.conf); blank = noindex everywhere. Set
# here so envsubst always replaces it: an unset variable would be left in the file as text.
ENV PUBLIC_HOST=""
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
# 127.0.0.1, not localhost: in the Alpine image localhost resolves to ::1 first
# and nginx only listens on IPv4, so the probe would fail with "connection refused".
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
