APP_DIR := spring-petclinic-microservices
COMPOSE := docker compose --file $(APP_DIR)/docker-compose.yml
PYTHON ?= $(shell command -v python3.11 || command -v python3)
# Everything except the GenAI chatbot, which needs an OpenAI key.
MODULES := !spring-petclinic-genai-service

.PHONY: build up down status targets kill-switch dashboard dashboard-demo bench doctor demo test report

build: ## Build the PetClinic images from this repo's source for this machine's CPU, plus the chaos helper image
	cd $(APP_DIR) && ./mvnw -q clean install -P buildDocker -DskipTests \
		-pl '$(MODULES)' \
		-Dcontainer.platform=linux/$(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
	docker build -q -t chaoslab/nettools tools/nettools

up: ## Start the whole sandbox and wait until it is healthy (add PROFILES=cadvisor for container metrics)
	$(COMPOSE) $(if $(PROFILES),--profile $(PROFILES)) up -d --build --wait

down: ## Stop and remove the sandbox
	$(COMPOSE) down

status: ## Show container health
	$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}'

targets: ## List the containers the chaos controller is allowed to fault
	$(PYTHON) -m chaoslab targets

kill-switch: ## Revert every active fault and restore the sandbox
	$(PYTHON) -m chaoslab stop

dashboard: ## Refresh the read-only dashboard from the newest finished local run
	$(PYTHON) -m chaoslab dashboard

dashboard-demo: ## Refresh the dashboard from the committed recorded Visits example
	$(PYTHON) -m chaoslab dashboard --demo

bench: ## Serve the live Resilience Test Bench on http://127.0.0.1:8090
	$(PYTHON) -m chaoslab bench

doctor: ## Check that the sandbox, traffic, monitoring, map and AI model are ready
	$(PYTHON) -m chaoslab doctor

demo: ## Check readiness, run an AI-planned campaign of 3 experiments, then write the report
	$(PYTHON) -m chaoslab doctor && $(PYTHON) -m chaoslab auto --budget 3 && $(PYTHON) -m chaoslab report

report: ## Ranked weaknesses, fixes and the A-F scorecard from every saved run
	$(PYTHON) -m chaoslab report

test: ## Run the offline test suite (no Docker needed)
	$(PYTHON) -m unittest discover -s tests
