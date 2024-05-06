PROJECT_NAME = "email2rss"
all: 
	docker build -t ${PROJECT_NAME} .
	
run:
	docker run --rm -it -p 8000:8000/tcp --env-file .env ${PROJECT_NAME}:latest

serve:
	docker run -d -p 8000:8000/tcp --env-file .env ${PROJECT_NAME}:latest

shell:
	docker exec -it ${PROJECT_NAME}:latest /bin/bash

clean:
	rm -rf __pycache__/ rss_feed email.db
