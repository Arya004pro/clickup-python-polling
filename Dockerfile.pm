# PM-ready image: bakes monitoring_config and project_map directly
# into the image so no volume mounts or extra files are needed.
#
# Build with:
#   docker build -t arya004/clickup-mcp-pm:latest -f Dockerfile.pm .
#   docker push arya004/clickup-mcp-pm:latest

FROM arya004/clickup-mcp-pm:latest

# Bake config files into the image
COPY monitoring_config.json /app/monitoring_config.json
COPY project_map.json /app/project_map.json
COPY report_spaces_config.json /app/report_spaces_config.json
