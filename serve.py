from waitress import serve
from my_sep import app
import logging

logging.basicConfig(level=logging.INFO)


serve(
    app,
    host="127.0.0.1",
    port=8000,
    threads=4,
    connection_limit=6,
)

