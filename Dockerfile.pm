# PM-ready image: bakes monitoring_config and project_map directly
# into the image so no volume mounts or extra files are needed.
#
# Build with:
#   docker build -t arya004pro/clickup-mcp-pm:latest -f Dockerfile.pm .
#   docker push arya004pro/clickup-mcp-pm:latest

FROM arya004pro/clickup-mcp:latest

# Bake config files into the image
COPY monitoring_config.json /app/monitoring_config.json
COPY project_map.json /app/project_map.json
