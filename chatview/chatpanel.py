import logging
import sublime

LOG = logging.getLogger("TermMate")


class RewindConfirmPanel:
    """
    Shows a floating popup (above all text) when hovering a prompt gutter dot.
    Confirm triggers the rewind; Cancel or moving away dismisses it.
    """

    def __init__(self, view):
        self.view = view
        self._on_confirm = None
        self._visible = False

    def show(self, region, prompt_index, on_confirm):
        """Show the popup anchored at the start of the prompt region."""
        self._on_confirm = on_confirm
        self._visible = True

        html = f"""
        <body id="chatview-rewind-confirm">
            <style>
                body {{ margin: 0; padding: 0; }}
                .dialog {{
                    padding: 16px 18px 14px 18px;
                    min-width: 260px;
                }}
                .title {{
                    color: var(--orangish);
                    font-weight: bold;
                    font-size: 1em;
                    font-family: var(--font-mono);
                    margin-bottom: 4px;
                }}
                .subtitle {{
                    color: color(var(--foreground) alpha(0.6));
                    font-size: 0.82em;
                    margin-bottom: 14px;
                }}
                .actions {{
                    margin-top: 4px;
                    margin-bottom: 4px;
                }}
                .btn {{
                    display: inline-block;
                    text-decoration: none;
                    padding: 5px 16px;
                    border-radius: 3px;
                    font-weight: bold;
                    font-size: 0.88em;
                    margin-right: 8px;
                }}
                .btn-confirm {{
                    background-color: var(--orangish);
                    color: var(--background);
                }}
                .btn-cancel {{
                    background-color: color(var(--background) blend(var(--foreground) 75%));
                    color: var(--foreground);
                    border: 1px solid color(var(--foreground) alpha(0.2));
                    margin-left: 8px;
                }}
            </style>
            <div class="dialog">
                <div class="title">↩ Restore conversation</div>
                <div class="subtitle">Restore files and conversaction to this point, later message will be discarded.</div>
                <div class="actions">
                    <a href="confirm" class="btn btn-confirm">Restore</a>
                    <a href="cancel" class="btn btn-cancel">Cancel</a>
                </div>
            </div>
        </body>
        """

        def on_navigate(href):
            callback = self._on_confirm
            self.clear()
            if href == "confirm" and callback:
                callback()

        def on_hide():
            self._visible = False
            self._on_confirm = None

        self.view.show_popup(
            html,
            location=region.begin(),
            flags=0,
            max_width=560,
            on_navigate=on_navigate,
            on_hide=on_hide,
        )

    def clear(self):
        """Dismiss the popup."""
        self._visible = False
        self._on_confirm = None
        self.view.hide_popup()

    @property
    def visible(self):
        return self._visible
