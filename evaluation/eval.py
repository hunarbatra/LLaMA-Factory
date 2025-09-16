import os
import re
import ast
import math
import random
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Union
import torch
from datasets import load_dataset, Value
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info
import json
from tqdm import tqdm
from PIL import Image
import requests
from io import BytesIO
import argparse
from mathruler.grader import grade_answer
from pathlib import Path
from enum import Enum

from templates import SPATIAL_THINKER_TEMPLATE

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('evaluation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatasetType(Enum):
    MATHVISTA = "mathvista"
    MATHVERSE = "mathverse"
    MATHVISION = "mathvision"
    SFTSEED = "sftseed"
    HALLUSIONBENCH = "hallusionbench"
    EMMA_MATH = "emma-math"
    EMMA_CHEM = "emma-chem"
    EMMA_CODE = "emma-code"
    EMMA_PHYSICS = "emma-physics"
    MMMU_PRO_VISION = "mmmu-pro-vision"
    CV_BENCH = "cv-bench"
    CV_BENCH_2D = "cv-bench-2D"
    CV_BENCH_3D = "cv-bench-3D"
    BENCH_3DSR = "3dsrbench"
    BENCH_3DSR_FULL = "3dsrbench_full"
    BLINK_SPATIAL = "blink-spatial"
    BLINK_DEPTH = "blink-depth"
    BLINK_OBJECT = "blink-object"
    BLINK_COUNTING = "blink-counting"
    BLINK_MULTI_VIEW = "blink-multi-view"
    REALWORLD_QA = "realworld_qa"
    SPATIALBENCH = "spatialbench"
    MMVP = "mmvp"
    LEGO = "lego"
    MATHVISTA_MCQ = "mathvista_mcq"
    MATHVERSE_VISION_MCQ = "mathverse_vision_mcq"
    MMMU_PRO = "mmmu_pro" # standard
    MMMU_PRO_VISION_ONLY = "mmmu_pro_vision_only"

@dataclass
class DatasetConfig:
    name: str
    split: str
    image_field: str # image field
    response_field: str # answer field
    instruction_field: Optional[str] = None # question field
    subset: Optional[str] = None
    choices_field: Optional[str] = None
    options_field: Optional[str] = None
    task_field: Optional[str] = None
    tasks: Optional[Dict[str, int]] = None

@dataclass
class ModelConfig:
    model_name: str
    processor_name: str
    max_new_tokens: int = 2048
    do_sample=False
    use_cache=True
    # top_p: float = 0.001
    # top_k: int = 1
    temperature: float = None
    # repetition_penalty: float = 1.0

class ImageProcessor:
    def __init__(self, model_config: ModelConfig, device: str):
        self.device = device
        self.model_config = model_config
        self.model = self._load_model()
        self.processor = self._load_processor()

    def _load_model(self) -> Qwen2_5_VLForConditionalGeneration:
        try:
            return Qwen2_5_VLForConditionalGeneration.from_pretrained(
                self.model_config.model_name,
                torch_dtype=torch.bfloat16,
                attn_implementation="flash_attention_2",
                device_map=self.device
            )
        except Exception as e:
            logger.error(f"Failed to load model: {str(e)}")
            raise

    def _load_processor(self) -> AutoProcessor:
        try:
            processor = AutoProcessor.from_pretrained(self.model_config.processor_name)
            processor.tokenizer.padding_side = 'left'
            return processor
        except Exception as e:
            logger.error(f"Failed to load processor: {str(e)}")
            raise

    def generate_answer(self, image_url: str | list[str], instruction: str) -> Optional[str]:
        try:
            if isinstance(image_url, list):
                content = [
                    {"type": "image", "image": url} for url in image_url
                ]
                content += [{"type": "text", "text": instruction}]
                messages = [
                    {
                        "role": "user",
                        "content": content,
                    }
                ]
            else:
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "image": image_url},
                            {"type": "text", "text": instruction},
                        ],
                    }
                ]
            
            text = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = self.processor(
                text=[text],
                images=image_inputs,
                videos=video_inputs,
                padding=True,
                return_tensors="pt",
            ).to(self.device)
            
            generated_ids = self.model.generate(
                **inputs,
                do_sample=self.model_config.do_sample,
                max_new_tokens=self.model_config.max_new_tokens,
                # top_p=self.model_config.top_p,
                # top_k=self.model_config.top_k,
                temperature=self.model_config.temperature,
                # repetition_penalty=self.model_config.repetition_penalty,
            )
            
            generated_ids_trimmed = [
                out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            return self.processor.batch_decode(
                generated_ids_trimmed,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False
            )[0]
        
        except Exception as e:
            logger.error(f"Error generating answer: {str(e)}")
            return None

    def generate_batch_answers(self, batch_items: List[Dict]) -> List[Optional[str]]:
        try:
            all_messages = []
            all_texts = []
            
            for item in batch_items:
                image_url = item['image_url']
                instruction = item['instruction']
                
                if isinstance(image_url, list):
                    content = [
                        {"type": "image", "image": url} for url in image_url
                    ]
                    content += [{"type": "text", "text": instruction}]
                    messages = [
                        {
                            "role": "user",
                            "content": content,
                        }
                    ]
                else:
                    messages = [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image", "image": image_url},
                                {"type": "text", "text": instruction},
                            ],
                        }
                    ]
                
                all_messages.append(messages)
                text = self.processor.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True
                )
                all_texts.append(text)
            
            all_image_inputs = []
            all_video_inputs = []
            
            for messages in all_messages:
                image_inputs, video_inputs = process_vision_info(messages)
                all_image_inputs.extend(image_inputs or [])
                all_video_inputs.extend(video_inputs or [])
            
            inputs = self.processor(
                text=all_texts,
                images=all_image_inputs if all_image_inputs else None,
                videos=all_video_inputs if all_video_inputs else None,
                padding=True,
                return_tensors="pt",
            ).to(self.device)
            
            generated_ids = self.model.generate(
                **inputs,
                do_sample=self.model_config.do_sample,
                max_new_tokens=self.model_config.max_new_tokens,
                # top_p=self.model_config.top_p,
                # top_k=self.model_config.top_k,
                temperature=self.model_config.temperature,
                # repetition_penalty=self.model_config.repetition_penalty,
            )
            
            generated_ids_trimmed = [
                out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            answers = self.processor.batch_decode(
                generated_ids_trimmed,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False
            )
            
            return answers
        
        except Exception as e:
            logger.error(f"Error generating batch answers: {str(e)}")
            return [None] * len(batch_items)

def get_dataset_config(dataset_type: DatasetType) -> DatasetConfig:
    configs = {
        DatasetType.MMVP: DatasetConfig(
            name="hunarbatra/MMVP",
            split="train",
            image_field="image",
            instruction_field="text",
            response_field="label"
        ),
        DatasetType.SPATIALBENCH: DatasetConfig(
            name="hunarbatra/SpatialBench",
            split="train",
            image_field="image",
            instruction_field="text",
            response_field="answer",
            task_field="category",
            tasks={"existence": 40, "reach": 40, "size": 40, "positional": 34, "counting": 20}
        ),
        DatasetType.REALWORLD_QA: DatasetConfig(
            name="visheratin/realworldqa",
            split="test",
            image_field="image",
            instruction_field="question",
            response_field="answer"
        ),
        DatasetType.BLINK_OBJECT: DatasetConfig(
            name="BLINK-Benchmark/BLINK",
            split="val",
            subset="Object_Localization",
            image_field="image_1",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
        ),
        DatasetType.BLINK_COUNTING: DatasetConfig(
            name="BLINK-Benchmark/BLINK",
            split="val",
            subset="Counting",
            image_field="image_1",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
        ),
        DatasetType.BLINK_SPATIAL: DatasetConfig(
            name="BLINK-Benchmark/BLINK",
            split="val",
            subset="Spatial_Relation",
            image_field="image_1",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
        ),
        DatasetType.BLINK_DEPTH: DatasetConfig(
            name="BLINK-Benchmark/BLINK",
            split="val",
            subset="Relative_Depth",
            image_field="image_1",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
        ),
        DatasetType.BLINK_MULTI_VIEW: DatasetConfig(
            name="BLINK-Benchmark/BLINK",
            split="val",
            subset="Multi-view_Reasoning",
            image_field=["image_1", "image_2"],
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
        ),
        DatasetType.BENCH_3DSR: DatasetConfig(
            name="hunarbatra/3DSRBench",
            split="test",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="task",
            tasks={"height": 350, "location": 875, "orientation": 525, "multi_object": 875} 
        ),
        DatasetType.BENCH_3DSR_FULL: DatasetConfig(
            name="hunarbatra/3DSRBench-Full",
            split="test",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="task",
            tasks={"height": 350, "location": 875, "orientation": 525, "multi_object": 875} 
        ),
        DatasetType.LEGO: DatasetConfig(
            name="hunarbatra/LEGO-Puzzles",
            split="train",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="category",
            tasks={"height": 100, "adjacency": 100, "rotation": 100, "multi_view": 100, "rotation_status": 100, "position": 100, "next_step": 100, "outlier": 100, "dependency": 100, "backwards": 100}
        ),
        DatasetType.MATHVISTA_MCQ: DatasetConfig(
            name="hunarbatra/MathVista_MCQ",
            split="testmini",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices"
        ),
        DatasetType.MATHVERSE_VISION_MCQ: DatasetConfig(
            name="hunarbatra/MathVerse_Vision_MCQ",
            split="testmini",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices"
        ),
        DatasetType.CV_BENCH: DatasetConfig(
            name="nyu-visionx/CV-Bench",
            split="test",
            subset="default",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="task",
            tasks={"Count": 788, "Relation": 650, "Distance": 600, "Depth": 600}
        ),
        DatasetType.CV_BENCH_2D: DatasetConfig(
            name="nyu-visionx/CV-Bench",
            split="test",
            subset="2D",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="task",
            tasks={"Count": 788, "Relation": 650}
        ),
        DatasetType.CV_BENCH_3D: DatasetConfig(
            name="nyu-visionx/CV-Bench",
            split="test",
            subset="3D",
            image_field="image",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="task",
            tasks={"Distance": 600, "Depth": 600}
        ),
        DatasetType.MMMU_PRO: DatasetConfig(
            name="MMMU/MMMU_Pro",
            split="test",
            subset="standard (4 options)",
            image_field="image_1",
            instruction_field="question",
            response_field="answer",
            choices_field="options",
            task_field="topic_difficulty",
            tasks={"Easy": 528, "Medium": 801, "Hard": 401}
        ),
        DatasetType.MMMU_PRO_VISION_ONLY: DatasetConfig(
            name="MMMU/MMMU_Pro",
            split="test",
            subset="vision",
            image_field="image",
            # instruction_field="question",
            response_field="answer",
            choices_field="options",
        ),
        DatasetType.MATHVISTA: DatasetConfig(
            name="AI4Math/MathVista",
            split="testmini",
            image_field="decoded_image",
            instruction_field="query",
            response_field="answer",
            choices_field="choices"
        ),
        DatasetType.MATHVERSE: DatasetConfig(
            name="AI4Math/MathVerse",
            subset="testmini",
            split="testmini",
            image_field="image",
            instruction_field="query_cot",
            response_field="answer"
        ),
        DatasetType.MATHVISION: DatasetConfig(
            name="MathLLMs/MathVision",
            split="test",
            image_field="decoded_image",
            instruction_field="question",
            response_field="answer",
            options_field="options"
        ),
        DatasetType.SFTSEED: DatasetConfig(
            name="ydeng9/sft_seed",
            split="train",
            image_field="decoded_image",
            instruction_field="problem",
            response_field="answer"
        ),
        DatasetType.HALLUSIONBENCH: DatasetConfig(
            name="lmms-lab/HallusionBench",
            split="image",
            image_field="image",
            instruction_field="question",
            response_field="gt_answer"
        ),
        DatasetType.EMMA_MATH: DatasetConfig(
            name="hunarbatra/EMMA_MATH",
            split="test",
            image_field="image_1",
            instruction_field="prompt",
            response_field="answer",
            choices_field="choices",
            task_field="category",
            tasks={"3D Spatial Simulation": 275, "2D Transformation": 266, "Path Tracing": 127, "Multi-hop Visual Object Counting": 124, "Pattern Inference": 100}
        ),
        DatasetType.EMMA_CHEM: DatasetConfig(
            name="luckychao/EMMA",
            subset="Chemistry",
            split="test",
            image_field=["image_1","image_2","image_3","image_4","image_5"],
            instruction_field="question",
            response_field="answer",
            options_field="options"
        ),
        DatasetType.EMMA_CODE: DatasetConfig(
            name="luckychao/EMMA",
            subset="Coding",
            split="test",
            image_field=["image_1","image_2","image_3","image_4","image_5"],
            instruction_field="question",
            response_field="answer",
            options_field="options"
        ),
        DatasetType.EMMA_PHYSICS: DatasetConfig(
            name="luckychao/EMMA",
            subset="Physics",
            split="test",
            image_field=["image_1","image_2","image_3","image_4","image_5"],
            instruction_field="question",
            response_field="answer",
            options_field="options"
        ),
        DatasetType.MMMU_PRO_VISION: DatasetConfig(
            name="MMMU/MMMU_Pro",
            subset="vision",
            split="test",
            image_field="image",
            response_field="answer",
            options_field="options"
        ),
    }
    return configs[dataset_type]

def load_image_dataset(dataset_config: DatasetConfig) -> List[Dict]:
    try:
        if dataset_config.subset:
            data = load_dataset(dataset_config.name, dataset_config.subset, split=dataset_config.split)
        else:
            data = load_dataset(dataset_config.name, split=dataset_config.split)
            
        if dataset_config.choices_field and data.features[dataset_config.choices_field] == Value("string"): # e.g. needed for MMMU_Pro and MMMU_Pro_Vision_Only
            data = data.map(lambda x: {dataset_config.choices_field: ast.literal_eval(x[dataset_config.choices_field])})
            print(f'Updated dataset choices field to list: {data.features[dataset_config.choices_field]}')
        
        items = []
        for item in data:
            if isinstance(dataset_config.image_field, list):
                dataset_item = {
                    'image_url': [item.get(x) for x in dataset_config.image_field if item.get(x) is not None],
                    'instruction': item.get(dataset_config.instruction_field, ''),
                    'response': item.get(dataset_config.response_field, ''),
                }
            else:
                dataset_item = {
                    'image_url': item[dataset_config.image_field],
                    'instruction': item.get(dataset_config.instruction_field, ''),
                    'response': item.get(dataset_config.response_field, ''),
                }
            if dataset_config.choices_field:
                dataset_item['choices'] = item.get(dataset_config.choices_field)
            if dataset_config.options_field:
                dataset_item['options'] = item.get(dataset_config.options_field, [])
            if dataset_config.task_field:
                dataset_item['task'] = item.get(dataset_config.task_field)
            items.append(dataset_item)
        return items
    except Exception as e:
        logger.error(f"Failed to load dataset: {str(e)}")
        raise

def save_descriptions(descriptions: List[Dict], output_file: str) -> None:
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w') as f:
            json.dump(descriptions, f, indent=2)
        logger.info(f"Saved {len(descriptions)} descriptions to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save descriptions: {str(e)}")
        raise

def process_response(response: str, choices: Optional[List[str]], options: Optional[List[str]] = None) -> str:
    if choices is not None:
        try:
            response_index = choices.index(response)
            return ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'][response_index]
        except ValueError:
            pass
    if options is not None and len(options) > 0:
        try:
            response_index = options.index(response)
            return ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'][response_index]
        except ValueError:
            pass
    return response

def format_instruction(instruction: str, options: Optional[List[str]] = None, choices: Optional[List[str]] = None, image_url: Optional[str] = None, yes_no: bool = False, vision: bool = False, spatial_thinker: bool = False, reasoning: bool = False, reasoning_end: bool = False, no_reasoning: bool = False) -> str:
    if vision:
        prompt_hint = "Hint: Please answer the question shown in the image."
        if options and len(options) > 0:
            prompt_hint += " Provide the correct option letter, e.g., A, B, C, D, E, at the end."
            choice_list = "\n".join(f"({chr(65+i)}) {opt}" for i, opt in enumerate(options))
            return f"{prompt_hint}\nChoices:\n{choice_list}"
        return prompt_hint
    elif yes_no:
        prompt_hint = "Hint: Please answer the question requiring an answer of yes or no."
        return f"{prompt_hint}\nQuestion: {instruction}"
    elif reasoning:
        # prompt_hint = "First output the thinking process in <think> </think> tags, followed by the final answer within <answer> </answer> tags."
        # prompt_hint = "You FIRST think about the reasoning process as an internal monologue and then provide the final answer. The reasoning process MUST BE enclosed within <think> </think> tags. The final answer MUST BE put within <answer> </answer> tags, and only return the final choice within the answer tags, e.g., <answer> {{correct_answer_choice}} </answer>."
        prompt_hint = "You FIRST think about the reasoning process as an internal monologue and then provide the final answer. The reasoning process MUST BE enclosed within <think> </think> tags. The final answer MUST BE put within <answer> </answer> tags, and only return the final choice including the correct option and answer within the answer tags, e.g., <answer> ({correct_option}) {correct_answer} </answer>."
        return f"{prompt_hint}\nQuestion: {instruction}"
    elif no_reasoning:
        return f"Question: {instruction}"
    elif reasoning_end:
        prompt_hint = "First output the thinking process in <think> </think> tags, followed by the final answer within <answer> </answer> tags."
        return f"Question: {instruction}\n{prompt_hint}"
    elif spatial_thinker: 
        prompt_prefix = SPATIAL_THINKER_TEMPLATE
        if isinstance(image_url, list):
            width, height = image_url[0].size
        else:
            width, height = image_url.size
        question = f"({width} x {height})\n\nNow answer the following question:\n{instruction}"
        return f"{prompt_prefix}\n{question}"
    elif options and len(options) > 0:
        prompt_hint = "Hint: Please answer the question and provide the correct option letter, e.g., A, B, C, D, E, at the end."
        choice_list = "\n".join(f"({chr(65+i)}) {opt}" for i, opt in enumerate(options))
        return f"{prompt_hint}\nQuestion: {instruction}\nChoices:\n{choice_list}"
    else:
        prompt_hint = "Hint: Please answer the question requiring an answer."
        return f"{prompt_hint}\nQuestion: {instruction}"
    
def extract_answer(correct_answer: str, pred_answer: str) -> bool:
    # check for the case where the answer is in the format (A) {...} etc
    if "(" in pred_answer and ")" in pred_answer:
        pred_answer = pred_answer.split("(")[1].split(")")[0]
    if len(pred_answer) and pred_answer[-1] == ".":
        pred_answer = pred_answer[:-1]
    return pred_answer.strip().lower() == correct_answer.strip().lower()

def main():
    parser = argparse.ArgumentParser(description='Evaluate model on various math datasets')
    parser.add_argument('--cuda', type=int, default=0, help='CUDA device number to use')
    parser.add_argument('--batch_size', type=int, default=1, help='Batch size for processing')
    parser.add_argument('--dataset', type=str, choices=['mathvista', 'mathverse', 'mathvision', 'sftseed', 'hallusionbench', 'emma-math', 'emma-chem', 'emma-code', 'emma-physics', 'mmmu-pro-vision', 'cv-bench', 'cv-bench-2D', 'cv-bench-3D', 'blink-spatial', 'blink-depth', 'blink-object', 'blink-counting', 'blink-multi-view', 'realworld_qa', 'spatialbench', 'mmvp', '3dsrbench', '3dsrbench_full', 'lego', 'mathvista_mcq', 'mathverse_vision_mcq', 'mmmu_pro', 'mmmu_pro_vision_only'],
                      default='cv-bench', help='Dataset to evaluate on')
    parser.add_argument('--model_path', type=str, help='Path to the model', default="Qwen/Qwen2.5-VL-3B-Instruct")
    parser.add_argument('--num_samples', type=int, default=None, help='Number of samples to evaluate')
    parser.add_argument('--custom_filename', type=str, default=None, help='Custom filename for output')
    parser.add_argument('--template', choices=['vision', 'yes_no', 'reasoning', 'spatial_thinker', 'reasoning_end', 'no_reasoning'], type=str, default="")
    parser.add_argument('--resume', action='store_true', help='Resume running evals')
    args = parser.parse_args()
    
    device = f"cuda:{args.cuda}" if torch.cuda.is_available() else "cpu"
    logger.info(f"Using device: {device}")

    # Configuration
    dataset_type = DatasetType(args.dataset)
    dataset_config = get_dataset_config(dataset_type)
    model_config = ModelConfig(
        model_name=args.model_path,
        processor_name="Qwen/Qwen2.5-VL-3B-Instruct" # TODO: switch to AutoProcessor
    )
    
    if args.custom_filename:
        output_file = f"./evaluation/outputs/{args.custom_filename}.json"
    else: 
        output_file = f"./evaluation/outputs/{dataset_type.value}_{model_config.model_name.split('/')[-1]}.json"
    
    descriptions = []
    correct = 0
    
    if args.resume and os.path.exists(output_file):
        with open(output_file, 'r') as f:
            descriptions = json.load(f)
        print(f"Resuming from {len(descriptions)} samples")
        
    # Initialize processor and model
    logger.info(f"Loading model {model_config.model_name}")
    processor = ImageProcessor(model_config, device)
    
    # Load dataset
    logger.info(f"Loading dataset {dataset_config.name}")
    data = load_image_dataset(dataset_config)
    if args.num_samples:
        data = data[:args.num_samples]
    if args.resume and len(descriptions) > 0:
        print(f'Already processed {len(descriptions)} samples')
        data = data[len(descriptions):]
        print(f'New dataset size: {len(data)}')
        
    # shuffle dataset
    if dataset_type != DatasetType.BENCH_3DSR_FULL:
        # we do not shuffle for 3DSRBench full as we compute the view-consistency scores
        random.seed(42)
        random.shuffle(data)

    # Process in batches
    batch_size = args.batch_size
    logger.info(f"Processing with batch size: {batch_size}")
    
    for batch_start in tqdm(range(0, len(data), batch_size), desc="Processing batches"):
        batch_end = min(batch_start + batch_size, len(data))
        batch_data = data[batch_start:batch_end]
        
        if args.template:
            template = args.template
        else:
            template = ""
        
        # Prepare batch instructions
        batch_items = []
        for item in batch_data:
            if dataset_type == DatasetType.MATHVISION or dataset_type == DatasetType.EMMA_CHEM or dataset_type == DatasetType.EMMA_CODE or dataset_type == DatasetType.EMMA_PHYSICS:
                formatted_instruction = format_instruction(item['instruction'], item.get('options'))
                
            # elif dataset_type == DatasetType.HALLUSIONBENCH:
            #     formatted_instruction = format_instruction(item['instruction'], yes_no=True)
                
            elif dataset_type == DatasetType.MMMU_PRO_VISION:
                formatted_instruction = format_instruction(item['instruction'], item.get('options'), vision=True)
                
            elif dataset_type in [DatasetType.CV_BENCH_2D, DatasetType.CV_BENCH_3D, DatasetType.CV_BENCH, DatasetType.BLINK_SPATIAL, DatasetType.BLINK_DEPTH, DatasetType.BLINK_COUNTING, DatasetType.BLINK_OBJECT, DatasetType.BLINK_MULTI_VIEW, DatasetType.BENCH_3DSR, DatasetType.BENCH_3DSR_FULL, DatasetType.MATHVISTA, DatasetType.EMMA_MATH, DatasetType.LEGO, DatasetType.MATHVISTA_MCQ, DatasetType.MATHVERSE_VISION_MCQ, DatasetType.HALLUSIONBENCH, DatasetType.MMMU_PRO, DatasetType.MMMU_PRO_VISION_ONLY]:
                if template == "reasoning":
                    formatted_instruction = format_instruction(item['instruction'], choices=item.get('choices'), image_url=item['image_url'], reasoning=True)
                elif template == "spatial_thinker":
                    formatted_instruction = format_instruction(item['instruction'], choices=item.get('choices'), image_url=item['image_url'], spatial_thinker=True)
                elif template == "no_reasoning":
                    formatted_instruction = format_instruction(item['instruction'], choices=item.get('choices'), image_url=item['image_url'], no_reasoning=True)
                else:
                    raise ValueError(f"Invalid template: {template}")
                if dataset_type == DatasetType.HALLUSIONBENCH:
                    formatted_instruction += "\nOptions:\n(A) Yes \n(B) No"
                if dataset_type == DatasetType.MMMU_PRO:
                    formatted_instruction += "\nOptions:" + "\n".join(f"({chr(i+65)}) {option}" for i, option in enumerate(item['choices']))
                if dataset_type == DatasetType.MMMU_PRO_VISION_ONLY:
                    prompt_hint = "Please answer the question shown in the image."
                    if args.template == "no_reasoning":
                        prompt_hint += " Please give the final answer within answer tags, e.g., <answer> ({correct_option}) {correct_answer} </answer>."
                    formatted_instruction += f"{prompt_hint}\nOptions:" + "\n".join(f"({chr(i+65)}) {option}" for i, option in enumerate(item['choices']))
            elif dataset_type == DatasetType.REALWORLD_QA:
                if template == "reasoning":
                    formatted_instruction = format_instruction(item['instruction'], image_url=item['image_url'], reasoning=True)
                elif template == "spatial_thinker":
                    remove_str = "Please answer directly with only the letter of the correct option and nothing else."
                    updated_instruction = item['instruction'].replace(remove_str, "") 
                    formatted_instruction = format_instruction(updated_instruction, image_url=item['image_url'], spatial_thinker=True)
                elif template == "reasoning_end":
                    formatted_instruction = format_instruction(item['instruction'], image_url=item['image_url'], reasoning_end=True)
                elif template == "no_reasoning":
                    formatted_instruction = format_instruction(item['instruction'], image_url=item['image_url'], no_reasoning=True)
                else:
                    raise ValueError(f"Invalid template: {template}")
            elif dataset_type == DatasetType.SPATIALBENCH or dataset_type == DatasetType.MMVP:
                updated_instruction = item['instruction']
                if updated_instruction[-1] != '.':
                    updated_instruction += '.'
                updated_instruction += ' Please give the final answer within answer tags.'
                if template == "reasoning":
                    formatted_instruction = format_instruction(updated_instruction, image_url=item['image_url'], reasoning=True)
                elif template == "spatial_thinker":
                    formatted_instruction = format_instruction(updated_instruction, image_url=item['image_url'], spatial_thinker=True)
                elif template == "no_reasoning":
                    formatted_instruction = format_instruction(updated_instruction, image_url=item['image_url'], no_reasoning=True)
                else:
                    raise ValueError(f"Invalid template: {template}")
            else:
                formatted_instruction = item['instruction']
            
            batch_items.append({
                'image_url': item['image_url'],
                'instruction': formatted_instruction,
                'original_item': item
            })
        
        # Generate answers for the batch
        if batch_size == 1:
            answers = [processor.generate_answer(batch_items[0]['image_url'], batch_items[0]['instruction'])]
        else:
            answers = processor.generate_batch_answers(batch_items)
            
        # print(f'answers: {answers}')
        
        # Process batch results
        for batch_idx, (batch_item, answer) in enumerate(zip(batch_items, answers)):
            i = batch_start + batch_idx
            item = batch_item['original_item']
            formatted_instruction = batch_item['instruction']
            
            correct_flag = 0
            reasoning = answer
            
            # handle cases when the model does not generate the response following the format i.e both answer tags are not used!
            if "Answer:" in answer:
                answer = answer.split("Answer:")[-1].strip()
            if answer and "<answer>" not in answer and "</answer>" not in answer:
                answer = f"<answer>{answer.strip()}</answer>"
            elif answer and "<answer>" not in answer and "</answer>" in answer:
                answer = answer.split("</answer>")[0].strip()
                answer = f"<answer>{answer}</answer>"
            
            if answer and "<answer>" in answer:
                if "</answer>" not in answer:
                    answer = answer.split("<answer>")[-1].strip()
                else:
                    answer = answer.split("<answer>")[-1].split("</answer>")[0].strip()
                if dataset_type == DatasetType.MMMU_PRO_VISION:
                    processed_response = item['response']
                elif dataset_type == DatasetType.REALWORLD_QA or dataset_type == DatasetType.SPATIALBENCH:
                    # ground truth answer
                    processed_response = item['response'].strip()
                    answer = answer.strip()
                elif dataset_type in [DatasetType.CV_BENCH_2D, DatasetType.CV_BENCH_3D, DatasetType.CV_BENCH, DatasetType.BLINK_SPATIAL, DatasetType.BLINK_DEPTH, DatasetType.BLINK_COUNTING, DatasetType.BLINK_OBJECT, DatasetType.BLINK_MULTI_VIEW, DatasetType.BENCH_3DSR, DatasetType.BENCH_3DSR_FULL, DatasetType.MATHVISTA, DatasetType.EMMA_MATH, DatasetType.LEGO, DatasetType.MATHVISTA_MCQ, DatasetType.MATHVERSE_VISION_MCQ, DatasetType.HALLUSIONBENCH, DatasetType.MMMU_PRO]:
                    # response is e.g. (A), remove braces, strip
                    
                    if dataset_type != DatasetType.MATHVISTA:
                        processed_response = item['response'].replace('(', '').replace(')', '').strip()
                    else:
                        processed_response = item['response'].strip()
                    
                    if dataset_type == DatasetType.HALLUSIONBENCH:
                        processed_response = processed_response.replace(".", "")
                        processed_response = "A" if processed_response == "1" else "B"

                    # map answer to choices index, and get its letter
                    choices = item.get('choices')
                    if dataset_type == DatasetType.HALLUSIONBENCH:
                        choices = ["Yes", "No"]
                        
                    if choices:
                        choices = [c.lower() for c in choices]
                        
                        # if processed_response is not a single letter, then from choices, find what's the correct answer index
                        if len(processed_response) > 1:
                            processed_response = processed_response[0].lower()
                            # processed_response_idx = choices.index(processed_response.lower())
                            # processed_response = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'][processed_response_idx]
                            
                        # from the generated answer, extract the answer post (A).. or A)..
                        pattern = r'^\([A-Za-z]\)\s*'
                        extracted_answer = re.sub(pattern, '', answer)
                        if extracted_answer == answer:
                            # test for pattern 2 to handle cases when answer is "A) .."
                            pattern2 = r'^\[A-Za-z]\)\s*'
                            extracted_answer = re.sub(pattern2, '', answer) 
                        if "{" in extracted_answer or "}" in extracted_answer:
                            extracted_answer = extracted_answer.replace("{", "").replace("}", "")
                        try: 
                            # fetch the predicted answers letter
                            answer_index = choices.index(extracted_answer.lower())
                            answer = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'][answer_index]
                        except ValueError: 
                            # if the answer string was not present, then that means we only have the final letter, so extract the single letter from generated answer
                            for j in range(len(choices)):
                                if f'({chr(65+j)})' in answer or f'{chr(65+j)})' or f'{chr(65+j)}.' in answer:
                                    answer = chr(65+j) 
                                    break
                else:
                    processed_response = process_response(
                        item['response'],
                        item.get('choices'),
                        item.get('options')
                    )
                
                
                if processed_response.lower() == answer.lower() or grade_answer(processed_response, answer) or extract_answer(processed_response, answer):
                    correct += 1
                    correct_flag = 1
            else:
                answer = "Failed to extract."
                logger.warning(f"Failed to extract answer for question {i}")
                processed_response = item['response']

            description = {
                'prompt': formatted_instruction,
                'correct_answer': processed_response,
                'reasoning': reasoning,
                'pred_answer': answer,
                'correct': correct_flag,
                'task': item.get('task')
            }        
            
            # compute task-wise scores
            if dataset_config.tasks and dataset_config.task_field:
                cur_task_name = item.get('task') # e.g. Count, Depth, etc
                
                for task_name, count in dataset_config.tasks.items():
                    if i > 0:
                        prev_task_correct = descriptions[i-1].get(f'{task_name}_correct', 0)
                    else:
                        prev_task_correct = 0
                    
                    if cur_task_name == task_name:
                        description[f'{task_name}_correct'] = prev_task_correct + correct_flag
                    else:
                        description[f'{task_name}_correct'] = prev_task_correct
                        
            if dataset_type == DatasetType.BENCH_3DSR_FULL:
                # compute view-consistency score which is based on the : curr even index if its even and the prev odd index, more like paired consistency, is current and prev both correct, then view-consistency gets a +1
                prev_view_consistency_correct_pairs = descriptions[i-1].get('view_consistency_correct_pairs', 0) if i > 0 else 0
                
                if i % 2 != 0 and i > 0:
                    prev_correct = descriptions[i-1].get('correct', 0)
                    if prev_correct == 1 and correct_flag == 1:
                        description['view_consistency'] = 1
                        descriptions[i-1]['view_consistency'] = 1 # prev even index view consistency
                        description['view_consistency_correct_pairs'] = prev_view_consistency_correct_pairs + 1
                    else:
                        description['view_consistency'] = 0
                        description['view_consistency_correct_pairs'] = prev_view_consistency_correct_pairs
                else:
                    description['view_consistency'] = 0
                    if i > 0:
                        description['view_consistency_correct_pairs'] = prev_view_consistency_correct_pairs
                        
            descriptions.append(description)
                        
            # Save periodically
            if (i + 1) % 10 == 0:
                cur_acc = correct / (i + 1)
                std_err = math.sqrt(cur_acc * (1 - cur_acc) / (i + 1))
                logger.info(f"Current accuracy: {cur_acc * 100:.2f} ± {std_err * 100:.2f}")
                description['cur_acc'] = cur_acc
                description['cur_std_err'] = std_err
                
                if dataset_config.tasks and dataset_config.task_field:
                    for task_name, count in dataset_config.tasks.items():
                        seen_so_far = sum(1 for d in descriptions if d.get('task') == task_name)
                        task_acc = descriptions[i-1].get(f'{task_name}_correct', 0) / max(1, seen_so_far)
                        
                        task_acc = min(max(task_acc, 0.0), 1.0)
                        
                        # task_std_err = math.sqrt(task_acc * (1 - task_acc) / max(1, seen_so_far))
                        variance_term = task_acc * (1 - task_acc) / max(1, seen_so_far)
                        task_std_err = math.sqrt(variance_term) if variance_term >= 0 else 0.0

                        description[f'{task_name}_acc'] = task_acc
                        description[f'{task_name}_std_err'] = task_std_err
                        logger.info(f"Current {task_name} accuracy: {task_acc * 100:.2f} ± {task_std_err * 100:.2f}")
                        
                if dataset_type == DatasetType.BENCH_3DSR_FULL:
                    pairs_total = (i + 1) // 2
                    pairs_consistent = descriptions[i-1].get('view_consistency_correct_pairs', 0) 
                    vc_acc = pairs_consistent / max(pairs_total, 1)
                    vc_std_err = math.sqrt(vc_acc * (1 - vc_acc) / max(pairs_total, 1))
                    description['view_consistency_acc'] = vc_acc
                    description['view_consistency_std_err'] = vc_std_err
                    logger.info(f"Current view consistency: {vc_acc * 100:.2f} ± {vc_std_err * 100:.2f}")
                
                descriptions[-1] = description
                save_descriptions(descriptions, output_file)
        
    # Final save
    accuracy = correct / len(data)
    std_err = math.sqrt(accuracy * (1 - accuracy) / len(data))
    
    description['final_accuracy'] = accuracy
    description['final_std_err'] = std_err

    if dataset_config.tasks and dataset_config.task_field:
        for task_name, count in dataset_config.tasks.items():
            task_acc = descriptions[-1].get(f'{task_name}_correct', 0) / min(count, len(data))
            task_acc = min(max(task_acc, 0.0), 1.0)
            description[f'{task_name}_final_acc'] = task_acc
            # task_std_err = math.sqrt(task_acc * (1 - task_acc) / min(count, len(data)))
            variance_term = task_acc * (1 - task_acc) / min(count, len(data))
            task_std_err = math.sqrt(variance_term) if variance_term >= 0 else 0.0
            description[f'{task_name}_final_std_err'] = task_std_err
            
    if dataset_type == DatasetType.BENCH_3DSR_FULL:
        pairs_total = (i + 1) // 2
        pairs_consistent = descriptions[i-1].get('view_consistency_correct_pairs', 0) 
        vc_acc = pairs_consistent / max(pairs_total, 1)
        vc_std_err = math.sqrt(vc_acc * (1 - vc_acc) / max(pairs_total, 1))
        description['final_view_consistency_acc'] = vc_acc
        description['final_view_consistency_std_err'] = vc_std_err
        logger.info(f"Final view consistency: {vc_acc * 100:.2f} ± {vc_std_err * 100:.2f}")
            
    descriptions[-1] = description
    save_descriptions(descriptions, output_file)
    
    logger.info(f"Completed! Final accuracy: {accuracy * 100:.2f} ± {std_err * 100:.2f}")
    if dataset_config.tasks and dataset_config.task_field:
        for task_name, count in dataset_config.tasks.items():
            task_acc = descriptions[-1].get(f'{task_name}_final_acc', 0)
            task_std_err = descriptions[-1].get(f'{task_name}_final_std_err', 0)
            logger.info(f"Final {task_name} accuracy: {task_acc * 100:.2f} ± {task_std_err * 100:.2f}")

if __name__ == "__main__":
    main() 
    