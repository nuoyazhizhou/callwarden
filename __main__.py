from .cli.console import ensure_utf8_output
ensure_utf8_output()
from .cli.main import main  # noqa: E402 —— ensure_utf8_output() 须先于 main 导入执行以固化 stdout 编码
main()
