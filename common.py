import logging
from dotenv import dotenv_values

config = dotenv_values(".env")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s - %(filename)s:%(lineno)d",
    handlers=[logging.FileHandler("email_to_rss.log"), logging.StreamHandler()],
)
