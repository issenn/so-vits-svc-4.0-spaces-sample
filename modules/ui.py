import gradio as gr
import modules.gradio as gr_mod

# gr.components.FormComponent
# gr.components.Form
# gr.components.Button
# gr.Button

refresh_symbol = '\U0001f504'  # 🔄


def create_refresh_button(refresh_component, refresh_method, refreshed_args, elem_id, **kwargs):
    def refresh(*args):
        refresh_method(*args)
        update_args = refreshed_args(*args) if callable(refreshed_args) else refreshed_args

        for k, v in update_args.items():
            setattr(refresh_component, k, v)

        return gr.update(**(update_args or {}))

    inputs = kwargs.get('inputs', None)
    refresh_button = gr_mod.Button(value=refresh_symbol, elem_id=elem_id)
    refresh_button.click(
        fn=refresh,
        inputs=inputs or [],
        outputs=[refresh_component]
    )
    return refresh_button


def create_refresh_func(refresh_component, refresh_method, refreshed_args):
    def refresh(*args):
        refresh_method(*args)
        update_args = refreshed_args(*args) if callable(refreshed_args) else refreshed_args

        for k, v in update_args.items():
            setattr(refresh_component, k, v)

        return gr.update(**(update_args or {}))
    return refresh
