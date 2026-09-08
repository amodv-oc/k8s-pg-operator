# syntax=docker/dockerfile:1.7

# --------------------------------------------------------------------------
# build: vite bundle
# --------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Lockfile first, in its own layer: dependencies change far less often than
# the source, so editing a component does not re-resolve the whole tree.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY index.html vite.config.js postcss.config.cjs ./
COPY src ./src
RUN npm run build

# --------------------------------------------------------------------------
# runtime: nginx serving the bundle and proxying the API on loopback
# --------------------------------------------------------------------------
FROM nginx:1.29-alpine AS runtime

LABEL org.opencontainers.image.title="pg-operator-ui" \
      org.opencontainers.image.description="Dashboard for the pg-operator state API" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/amodv-oc/k8s-pg-operator"

# The base image's entrypoint scripts template files into /etc/nginx, which
# readOnlyRootFilesystem forbids. nginx.conf below is complete, so they are
# removed and nginx is started directly.
RUN rm -rf /docker-entrypoint.d /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/nginx.conf
COPY api-proxy.conf /etc/nginx/api-proxy.conf
# Shadowed by the chart's ConfigMap; present so the image also runs standalone.
COPY config.default.json /etc/pg-operator-ui/config.json
COPY --from=build /app/dist /usr/share/nginx/html

# Same uid as the operator and API containers. Nothing here is owned by it:
# every writable path is under the /tmp the pod mounts in memory.
USER 65532:65532

EXPOSE 8081
STOPSIGNAL SIGQUIT

ENTRYPOINT ["nginx", "-g", "daemon off;"]
