# linux-gpio-http-server

## Development

Install dependencies:

`uv sync --all-groups`

Format code with Ruff:

`uv run ruff format .`

Check style/lint issues:

`uv run ruff check .`

## Run with waitress-serve

Set config path:

`export LINUX_GPIO_HTTP_SERVER_CONFIG=example.yaml`

Run via waitress:

`uv run waitress-serve --listen=0.0.0.0:8000 --call linux_gpio_http_server:create_app`
