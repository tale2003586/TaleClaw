from plugins.base import Plugin
from runtime.trace.report import write_markdown_report


class RunReportPlugin(Plugin):
    name = "run_report"

    def after_run(self, context) -> None:
        if context.run_dir is None:
            return
        write_markdown_report(context.run_dir)
