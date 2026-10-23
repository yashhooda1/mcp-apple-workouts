FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Remote transport for hosted deploys (Railway/Fly/Render)
ENV TRANSPORT=streamable-http
ENV HOST=0.0.0.0
ENV PORT=8000
# Mount a volume here so the feed survives restarts
ENV WORKOUT_FEED_DIR=/data/feed
ENV STRAVA_TOKENS=/data/strava_tokens.json
EXPOSE 8000

CMD ["apple-workouts-mcp"]
