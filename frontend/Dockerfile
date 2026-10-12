FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.29-alpine
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY infra/nginx/proxy.conf /etc/nginx/proxy.conf
COPY --from=build /app/dist /usr/share/nginx/html
