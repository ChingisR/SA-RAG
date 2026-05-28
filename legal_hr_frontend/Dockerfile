# Stage 1: Build the React application
FROM node:22-slim AS build
WORKDIR /app

# Set memory limit for Node.js to resolve 'rolldown' build crashes in restricted environments
ENV NODE_OPTIONS="--max-old-space-size=4096"

COPY package*.json ./
# ENSURE no legacy lockfile is used to prevent version mismatches
RUN rm -f package-lock.json

RUN npm install --legacy-peer-deps
COPY . .
RUN npm run build

# Stage 2: Serve the static files with Nginx
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
