# ============================================================================
#  Mary flow — the restored Node/Express "secretary" (db83a6a).
#  ONE process, TWO personas selected by trigger tag inside 1. Orchestrator/server.js:
#    @assistente / @assistant -> the frozen legacy dispatch (owner's daily driver)
#    @mary                    -> the orchestrator turn-loop
#  Listens on PORT (default 3000), single POST /webhook. Fed by ../dispatcher.
# ============================================================================
FROM node:20-alpine

WORKDIR /app

# Install deps first for layer caching. package.json declares express, the
# Anthropic SDK, googleapis and redis.
COPY package.json ./
RUN npm install --no-audit --no-fund

# The app tree (note the numbered, space-containing skill folders).
COPY . .

EXPOSE 3000
CMD ["npm", "start"]
