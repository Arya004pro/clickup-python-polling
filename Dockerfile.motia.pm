# PM-ready Motia image: bakes report_spaces_config and monitoring_config
FROM arya004/clickup-motia:latest

# Bake config files into the image so Motia steps can read them
COPY report_spaces_config.json /app/report_spaces_config.json
COPY monitoring_config.json /app/monitoring_config.json
