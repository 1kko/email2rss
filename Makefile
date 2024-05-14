PROJECT_NAME = "email2rss"

all: 
	docker build -t ${PROJECT_NAME} .
	docker image prune -f

build: 
	docker build -t ${PROJECT_NAME} .
	docker image prune -f
	
run:
	docker run --rm -it -p 8000:8000/tcp --env-file .env -v $(PWD)/data:/app/data ${PROJECT_NAME}:latest 

serve:
	docker run -d -p 8000:8000/tcp --env-file .env -v $(PWD)/data:/app/data ${PROJECT_NAME}:latest

shell:
	docker exec -it ${PROJECT_NAME}:latest /bin/bash

clean:
	dokcer image prune -f
	# rm -rf __pycache__/ data email.db ${PROJECT_NAME}.tar

export:
	docker save -o ${PROJECT_NAME}.tar ${PROJECT_NAME}:latest
	echo "Docker image saved as ${PROJECT_NAME}.tar"
	echo "To load the image, run: docker load -i ${PROJECT_NAME}.tar"