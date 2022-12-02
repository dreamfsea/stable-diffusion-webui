from collections import namedtuple
from copy import copy
from itertools import permutations, chain
import random
import csv
from io import StringIO
from PIL import Image
import numpy as np

import modules.scripts as scripts
import gradio as gr

from modules import images, sd_samplers
from modules.hypernetworks import hypernetwork
from modules.processing import process_images, Processed, StableDiffusionProcessingTxt2Img
from modules.shared import opts, cmd_opts, state
import modules.shared as shared
import modules.sd_samplers
import modules.sd_models
import re


def process_axis(opt, vals):
    if opt.label == 'Nothing':
        return [0]

    valslist = [x.strip() for x in chain.from_iterable(
        csv.reader(StringIO(vals)))]

    if opt.type == int:
        valslist_ext = []

        for val in valslist:
            m = re_range.fullmatch(val)
            mc = re_range_count.fullmatch(val)
            if m is not None:
                start = int(m.group(1))
                end = int(m.group(2))+1
                step = int(m.group(3)) if m.group(3) is not None else 1

                valslist_ext += list(range(start, end, step))
            elif mc is not None:
                start = int(mc.group(1))
                end = int(mc.group(2))
                num = int(mc.group(3)) if mc.group(3) is not None else 1

                valslist_ext += [int(x) for x in np.linspace(
                                start=start, stop=end, num=num).tolist()]
            else:
                valslist_ext.append(val)

        valslist = valslist_ext
    elif opt.type == float:
        valslist_ext = []

        for val in valslist:
            m = re_range_float.fullmatch(val)
            mc = re_range_count_float.fullmatch(val)
            if m is not None:
                start = float(m.group(1))
                end = float(m.group(2))
                step = float(m.group(3)) if m.group(
                            3) is not None else 1

                valslist_ext += np.arange(start,
                                        end + step, step).tolist()
            elif mc is not None:
                start = float(mc.group(1))
                end = float(mc.group(2))
                num = int(mc.group(3)) if mc.group(3) is not None else 1

                valslist_ext += np.linspace(start=start,
                                                    stop=end, num=num).tolist()
            else:
                valslist_ext.append(val)

        valslist = valslist_ext
    elif opt.type == str_permutations:
        valslist = list(permutations(valslist))
    elif opt.type == str_matrix_permutations:
        output = []
        prompt_matrix_parts = valslist
        combination_count = 2 ** (len(prompt_matrix_parts) - 1)
        for combination_num in range(combination_count):
            selected_prompts = [text.strip().strip(',') for n, text in enumerate(prompt_matrix_parts[1:]) if combination_num & (1 << n)]
            output.append((valslist[0]+", "+", ".join(selected_prompts)).strip(", "))
        valslist = output

    valslist = [opt.type(x) for x in valslist]
    return valslist

def axis_opt_name_find(name):
    for option in axis_options:
        if option.label.lower()==name.lower():
            return option
    print(f"could not find parameter option {name.lower()}") #should I have a lower() here?
    return None

def apply_field(field):
    def fun(p, x, xs):
        setattr(p, field, x)

    return fun


def apply_prompt(p, x, xs):
    if xs[0] not in p.prompt and xs[0] not in p.negative_prompt:
        raise RuntimeError(f"Prompt S/R did not find {xs[0]} in prompt or negative prompt.")

    p.prompt = remove_junk(p.prompt.replace(xs[0], x))
    p.negative_prompt = remove_junk(p.negative_prompt.replace(xs[0], x))
    

def SR_placeholder(p, x, xs):
    if xs[0] not in p.prompt and xs[0] not in p.negative_prompt:
        raise RuntimeError(f"Prompt S/R placeholder did not find {xs[0]} in prompt or negative prompt.")
    if x == xs[0]:
        x=""
    p.prompt = remove_junk(p.prompt.replace(xs[0], x))
    p.negative_prompt = remove_junk(p.negative_prompt.replace(xs[0], x))


def apply_multitool(p, x, xs):
    #xs is the strings variable, so it needs to be parsed like this into a list of lists
    fields = []
    data = []
    attributes=x.split(" | ")
    for attr in attributes:
        field, datum = attr.split(": ")
        fields.append(field)
        option = axis_opt_name_find(field)
        if option.type == int:
            data.append(int(datum))
        elif option.type == float:
            data.append(float(datum))
        elif option.type == str_permutations:
            data.append(datum.split(", "))
        else:
            data.append(datum)
    for ind in range(len(data)):
        datalist=[]
        for x in xs: #parse xs to get local xs
            attrs = x.split(" | ")
            for attr in attrs:
                field, datapiece = attr.split(": ")
                if field == fields[ind] and datapiece not in datalist:
                    option = axis_opt_name_find(fields[ind])
                    if option.type == int:
                        datalist.append(int(datapiece))
                    elif option.type == float:
                        datalist.append(float(datapiece))
                    elif option.type == str_permutations:
                        datalist.append(datapiece.split(", "))
                    else:
                        datalist.append(datapiece)
        opt_field = axis_opt_name_find(fields[ind])
        opt_field.apply(p, data[ind], datalist)
    #x will be like "Checkpoint name: 1.5 | Sampler: Euler a"
    return


def parse_multitool(parse_input):
    import itertools
    data=[]
    fields=[]
    #parse the typed input (this is the main parser that gets all the values)
    splitinput = parse_input.split("|")
    for param in splitinput:
        param=param.strip()
        field, datapiece = param.split(":")
        fields.append(axis_opt_name_find(field.strip()).label) #do this to get a consistent field name (with consistent capitalization) rather than whatever the user types
        data.append(datapiece.strip())
    newdata = []
    #parse the subinputs
    for ind in range(len(fields)):
        field_selected = axis_opt_name_find(fields[ind])
        processed_data = process_axis(field_selected, data[ind])
        if (field_selected.type == str_permutations):
            dummy = []
            for permutation in processed_data:
                dummy.append(", ".join(permutation))
            newdata.append(dummy)
        else:
            newdata.append(processed_data)
    data=newdata

    #find all combinations
    results = []  # collect your products
    for c in itertools.combinations(data, len(data)):
        for res in itertools.product(*c):
            results.append(res)
    #convert combinations to labels on graph
    strings = []
    for result in results:
        string = []
        for index in range(len(result)):
            string.append(f"{fields[index]}: {result[index]}")
        strings.append(" | ".join(string))
    return strings

def apply_order(p, x, xs):
    token_order = []

    # Initally grab the tokens from the prompt, so they can be replaced in order of earliest seen
    for token in x:
        token_order.append((p.prompt.find(token), token))

    token_order.sort(key=lambda t: t[0])

    prompt_parts = []

    # Split the prompt up, taking out the tokens
    for _, token in token_order:
        n = p.prompt.find(token)
        prompt_parts.append(p.prompt[0:n])
        p.prompt = p.prompt[n + len(token):]

    # Rebuild the prompt with the tokens in the order we want
    prompt_tmp = ""
    for idx, part in enumerate(prompt_parts):
        prompt_tmp += part
        prompt_tmp += x[idx]
    p.prompt = prompt_tmp + p.prompt
    
def apply_matrix(p, x, xs):
    x = [x.strip() for x in chain.from_iterable(
        csv.reader(StringIO(x)))]
    replace = x[0]
    if len(x)==1:
        replacewith = ""
    else:
        replacewith = ", ".join(x[1:])
    if replace not in p.prompt and replace not in p.negative_prompt:
        raise RuntimeError(f"Prompt matrix did not find {replace} in prompt or negative prompt.")
    p.prompt = remove_junk(p.prompt.replace(replace, replacewith))
    p.negative_prompt = remove_junk(p.negative_prompt.replace(replace, replacewith))
    #take first value in xs, and replace that string in prompt or negative_prompt with whatever is in x (unless x is the same as xs[0])

def remove_junk(input_string):
    output_string = input_string.replace(", ,", ", ")
    output_string = output_string.replace(",,", ", ")
    output_string = output_string.strip(", ")
    return output_string

def format_multitool(p, opt, x): #this is not used, but the code works
    attrs = x.split(" | ")
    formatted_list = []
    for attr in attrs:
        field, datapiece = attr.split(": ")
        field_opt = axis_opt_name_find(field)
        if field_opt.type == int:
            datapiece = int(datapiece)
        elif field_opt.type == float:
            datapiece = float(datapiece)
        datapiece = field_opt.format_value(p, field_opt, datapiece)
        formatted_list.append(', '.join((field, datapiece)))
    return ' | '.join(formatted_list)

def build_samplers_dict():
    samplers_dict = {}
    for i, sampler in enumerate(sd_samplers.all_samplers):
        samplers_dict[sampler.name.lower()] = i
        for alias in sampler.aliases:
            samplers_dict[alias.lower()] = i
    return samplers_dict


def apply_sampler(p, x, xs):
    sampler_index = build_samplers_dict().get(x.lower(), None)
    if sampler_index is None:
        raise RuntimeError(f"Unknown sampler: {x}")

    p.sampler_index = sampler_index


def confirm_samplers(p, xs):
    samplers_dict = build_samplers_dict()
    for x in xs:
        if x.lower() not in samplers_dict.keys():
            raise RuntimeError(f"Unknown sampler: {x}")


def apply_checkpoint(p, x, xs):
    info = modules.sd_models.get_closet_checkpoint_match(x)
    if info is None:
        raise RuntimeError(f"Unknown checkpoint: {x}")
    modules.sd_models.reload_model_weights(shared.sd_model, info)
    p.sd_model = shared.sd_model


def confirm_checkpoints(p, xs):
    for x in xs:
        if modules.sd_models.get_closet_checkpoint_match(x) is None:
            raise RuntimeError(f"Unknown checkpoint: {x}")


def apply_hypernetwork(p, x, xs):
    if x.lower() in ["", "none"]:
        name = None
    else:
        name = hypernetwork.find_closest_hypernetwork_name(x)
        if not name:
            raise RuntimeError(f"Unknown hypernetwork: {x}")
    hypernetwork.load_hypernetwork(name)


def apply_hypernetwork_strength(p, x, xs):
    hypernetwork.apply_strength(x)


def confirm_hypernetworks(p, xs):
    for x in xs:
        if x.lower() in ["", "none"]:
            continue
        if not hypernetwork.find_closest_hypernetwork_name(x):
            raise RuntimeError(f"Unknown hypernetwork: {x}")


def apply_clip_skip(p, x, xs):
    opts.data["CLIP_stop_at_last_layers"] = x


def format_value_add_label(p, opt, x):
    if type(x) == float:
        x = round(x, 8)

    return f"{opt.label}: {x}"


def format_value(p, opt, x):
    if type(x) == float:
        x = round(x, 8)
    return x


def format_value_join_list(p, opt, x):
    return ", ".join(x)


def do_nothing(p, x, xs):
    pass


def format_nothing(p, opt, x):
    return ""


def str_permutations(x):
    """dummy function for specifying it in AxisOption's type when you want to get a list of permutations"""
    return x

def str_matrix_permutations(x):
    """dummy function for specifying it in AxisOption's type when you want to get a list of permutations for a prompt matrix"""
    return x

AxisOption = namedtuple("AxisOption", ["label", "type", "apply", "format_value", "confirm"])
AxisOptionImg2Img = namedtuple("AxisOptionImg2Img", ["label", "type", "apply", "format_value", "confirm"])


axis_options = [
    AxisOption("Nothing", str, do_nothing, format_nothing, None),
    AxisOption("Seed", int, apply_field("seed"), format_value_add_label, None),
    AxisOption("Var. seed", int, apply_field("subseed"), format_value_add_label, None),
    AxisOption("Var. strength", float, apply_field("subseed_strength"), format_value_add_label, None),
    AxisOption("Steps", int, apply_field("steps"), format_value_add_label, None),
    AxisOption("CFG Scale", float, apply_field("cfg_scale"), format_value_add_label, None),
    AxisOption("Prompt S/R", str, apply_prompt, format_value, None),
    AxisOption("Prompt S/R Placeholder", str, SR_placeholder, format_value, None),
    AxisOption("Prompt Matrix", str_matrix_permutations, apply_matrix, format_value, None),
    AxisOption("Prompt order", str_permutations, apply_order, format_value_join_list, None),
    AxisOption("Sampler", str, apply_sampler, format_value, confirm_samplers),
    AxisOption("Checkpoint name", str, apply_checkpoint, format_value, confirm_checkpoints),
    AxisOption("Hypernetwork", str, apply_hypernetwork, format_value, confirm_hypernetworks),
    AxisOption("Hypernet str.", float, apply_hypernetwork_strength, format_value_add_label, None),
    AxisOption("Sigma Churn", float, apply_field("s_churn"), format_value_add_label, None),
    AxisOption("Sigma min", float, apply_field("s_tmin"), format_value_add_label, None),
    AxisOption("Sigma max", float, apply_field("s_tmax"), format_value_add_label, None),
    AxisOption("Sigma noise", float, apply_field("s_noise"), format_value_add_label, None),
    AxisOption("Eta", float, apply_field("eta"), format_value_add_label, None),
    AxisOption("Clip skip", int, apply_clip_skip, format_value_add_label, None),
    AxisOption("Denoising", float, apply_field("denoising_strength"), format_value_add_label, None),
    AxisOption("Multitool", str, apply_multitool, format_value, None)
]


def draw_xy_grid(p, xs, ys, x_labels, y_labels, cell, draw_legend, include_lone_images):
    ver_texts = [[images.GridAnnotation(y)] for y in y_labels]
    hor_texts = [[images.GridAnnotation(x)] for x in x_labels]

    # Temporary list of all the images that are generated to be populated into the grid.
    # Will be filled with empty images for any individual step that fails to process properly
    image_cache = []

    processed_result = None
    cell_mode = "P"
    cell_size = (1,1)

    state.job_count = len(xs) * len(ys) * p.n_iter

    for iy, y in enumerate(ys):
        for ix, x in enumerate(xs):
            state.job = f"{ix + iy * len(xs) + 1} out of {len(xs) * len(ys)}"

            processed:Processed = cell(x, y)
            try:
                # this dereference will throw an exception if the image was not processed
                # (this happens in cases such as if the user stops the process from the UI)
                processed_image = processed.images[0]
                
                if processed_result is None:
                    # Use our first valid processed result as a template container to hold our full results
                    processed_result = copy(processed)
                    cell_mode = processed_image.mode
                    cell_size = processed_image.size
                    processed_result.images = [Image.new(cell_mode, cell_size)]

                image_cache.append(processed_image)
                if include_lone_images:
                    processed_result.images.append(processed_image)
                    processed_result.all_prompts.append(processed.prompt)
                    processed_result.all_seeds.append(processed.seed)
                    processed_result.infotexts.append(processed.infotexts[0])
            except:
                image_cache.append(Image.new(cell_mode, cell_size))

    if not processed_result:
        print("Unexpected error: draw_xy_grid failed to return even a single processed image")
        return Processed()

    grid = images.image_grid(image_cache, rows=len(ys))
    if draw_legend:
        grid = images.draw_grid_annotations(grid, cell_size[0], cell_size[1], hor_texts, ver_texts)

    processed_result.images[0] = grid

    return processed_result


class SharedSettingsStackHelper(object):
    def __enter__(self):
        self.CLIP_stop_at_last_layers = opts.CLIP_stop_at_last_layers
        self.hypernetwork = opts.sd_hypernetwork
        self.model = shared.sd_model
  
    def __exit__(self, exc_type, exc_value, tb):
        modules.sd_models.reload_model_weights(self.model)

        hypernetwork.load_hypernetwork(self.hypernetwork)
        hypernetwork.apply_strength()

        opts.data["CLIP_stop_at_last_layers"] = self.CLIP_stop_at_last_layers


re_range = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\(([+-]\d+)\s*\))?\s*")
re_range_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\(([+-]\d+(?:.\d*)?)\s*\))?\s*")

re_range_count = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\[(\d+)\s*\])?\s*")
re_range_count_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\[(\d+(?:.\d*)?)\s*\])?\s*")

invalid_filename_chars = '<>:"/\\|?*\n'
invalid_filename_prefix = ' '
invalid_filename_postfix = ' .'
max_filename_part_length = 128

class Script(scripts.Script):
    def title(self):
        return "Extended X/Y plot"

    def ui(self, is_img2img):
        current_axis_options = [x for x in axis_options if type(x) == AxisOption or type(x) == AxisOptionImg2Img and is_img2img]

        with gr.Row():
            x_type = gr.Dropdown(label="X type", choices=[x.label for x in current_axis_options], value=current_axis_options[1].label, type="index", elem_id="x_type", interactive = True)
            x_values = gr.Textbox(label="X values", lines=1)

        with gr.Row():
            y_type = gr.Dropdown(label="Y type", choices=[x.label for x in current_axis_options], value=current_axis_options[0].label, type="index", elem_id="y_type", interactive = True)
            y_values = gr.Textbox(label="Y values", lines=1)
        
        with gr.Row():
            draw_legend = gr.Checkbox(label='Draw legend', value=True, visible=True)
            include_lone_images = gr.Checkbox(label='Include Separate Images', value=False, visible=True)
            no_fixed_seeds = gr.Checkbox(label='Keep -1 for seeds', value=False, visible=True)
            put_in_dir = gr.Checkbox(label='Put all individual images in a directory', value=False, visible=True)

        return [x_type, x_values, y_type, y_values, draw_legend, include_lone_images, no_fixed_seeds, put_in_dir]

    def run(self, p, x_type, x_values, y_type, y_values, draw_legend, include_lone_images, no_fixed_seeds, put_in_dir):
        if put_in_dir:
            prev_save_to_dirs = opts.save_to_dirs
            opts.save_to_dirs = True
            text = p.prompt
            text = text.translate({ord(x): '_' for x in invalid_filename_chars})
            text = text.lstrip(invalid_filename_prefix)[:max_filename_part_length]
            text = text.rstrip(invalid_filename_postfix)
            savedir_img2img = opts.outdir_img2img_samples
            savedir_txt2img = opts.outdir_txt2img_samples
            opts.outdir_img2img_samples = savedir_img2img+"/"+text
            opts.outdir_txt2img_samples = savedir_txt2img+"/"+text

        if not no_fixed_seeds:
            modules.processing.fix_seed(p)

        if not opts.return_grid:
            p.batch_size = 1
        xs = []
        x_opt = axis_options[x_type]
        if x_opt.label == "Multitool":
            xs = parse_multitool(x_values)
        else:
            xs = process_axis(x_opt, x_values)
        if x_opt.confirm:
            x_opt.confirm(p, xs)
        ys = []
        y_opt = axis_options[y_type]
        if y_opt.label == "Multitool":
            ys = parse_multitool(y_values)
        else:
            ys = process_axis(y_opt, y_values)
        if y_opt.confirm:
            y_opt.confirm(p, ys)
        def fix_axis_seeds(axis_opt, axis_list):
            if axis_opt.label in ['Seed','Var. seed']:
                return [int(random.randrange(4294967294)) if val is None or val == '' or val == -1 else val for val in axis_list]
            else:
                return axis_list

        #need to fix this too
        if not no_fixed_seeds:
            xs = fix_axis_seeds(x_opt, xs)
            ys = fix_axis_seeds(y_opt, ys)


        x_steps = []
        if x_opt.label == 'Steps':
            x_steps = xs
        elif x_opt.label == 'Multitool':
            for x in xs: #parse xs to get step counts for axis
                attrs = x.split(" | ")
                for attr in attrs:
                    field, datapiece = attr.split(": ")
                    if field == "Steps":
                        x_steps.append(int(datapiece))
        
        y_steps = []
        if y_opt.label == 'Steps':
            y_steps = ys
        elif y_opt.label == 'Multitool':
            for y in ys: #parse ys to get step counts for axis
                attrs = y.split(" | ")
                for attr in attrs:
                    field, datapiece = attr.split(": ")
                    if field == "Steps":
                        y_steps.append(int(datapiece))
        
        if x_steps != []:
            total_steps = len(ys)*sum(x_steps)
        elif y_steps != []:
            total_steps = len(xs)*sum(y_steps)
        else:
            total_steps = p.steps * len(xs) * len(ys)

        if isinstance(p, StableDiffusionProcessingTxt2Img) and p.enable_hr:
            total_steps *= 2

        print(f"Extended X/Y plot will create {len(xs) * len(ys) * p.n_iter} images on a {len(xs)}x{len(ys)} grid. (Total steps to process: {total_steps * p.n_iter})")
        shared.total_tqdm.updateTotal(total_steps * p.n_iter)

        def cell(x, y):
            pc = copy(p)
            x_opt.apply(pc, x, xs)
            y_opt.apply(pc, y, ys)

            return process_images(pc)

        with SharedSettingsStackHelper():
            processed = draw_xy_grid(
                p,
                xs=xs,
                ys=ys,
                x_labels=[x_opt.format_value(p, x_opt, x) for x in xs],
                y_labels=[y_opt.format_value(p, y_opt, y) for y in ys],
                cell=cell,
                draw_legend=draw_legend,
                include_lone_images=include_lone_images
            )
        infostring=f"""
            {p.prompt}
            Negative prompt: {p.negative_prompt}
            X: {axis_options[x_type].label}: {x_values}
            Y: {axis_options[y_type].label}: {y_values}
            Steps: {p.steps},
            CFG scale: {p.cfg_scale},
            Seed: {p.seed},
            Size: {p.width}x{p.height},
            Model hash: {getattr(p, 'sd_model_hash', None if not opts.add_model_hash_to_info or not shared.sd_model.sd_model_hash else shared.sd_model.sd_model_hash)},
            Model: {(None if not opts.add_model_name_to_info or not shared.sd_model.sd_checkpoint_info.model_name else shared.sd_model.sd_checkpoint_info.model_name.replace(',', '').replace(':', ''))},
            Sampler: {p.sampler_name}
        """
        if opts.grid_save:
            images.save_image(processed.images[0], p.outpath_grids, "xy_grid", prompt=p.prompt, seed=processed.seed, grid=True, p=p, info = infostring)

        if put_in_dir: #reset save path #TODO: I need to fix how saving works
            opts.outdir_img2img_samples = savedir_img2img
            opts.outdir_txt2img_samples = savedir_txt2img
            opts.save_to_dirs=prev_save_to_dirs
        return processed
