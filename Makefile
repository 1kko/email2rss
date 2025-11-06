PROJECT_NAME = "email2rss"

all: build

build:
	docker compose build

run:
	docker compose up

serve:
	docker compose up -d

stop:
	docker compose down

shell:
	docker compose exec fetch_and_generate /bin/bash

clean:
	docker compose down --rmi all
	# rm -rf __pycache__/ data email.db ${PROJECT_NAME}.tar

logs:
	docker compose logs -f

export:
	docker save -o ${PROJECT_NAME}.tar ${PROJECT_NAME}_fetch_and_generate:latest ${PROJECT_NAME}_serve:latest
	echo "Docker images saved as ${PROJECT_NAME}.tar"
	echo "To load the images, run: docker load -i ${PROJECT_NAME}.tar"
