from concurrent.futures import ThreadPoolExecutor, as_completed

from databench_eval import Runner, Evaluator, utils
from datasets import load_dataset
from dyna_gym.pipelines import uct_for_hf_transformer_pipeline
from py_evaluator.test_case_runner import test_run_code
from transformers import pipeline, AutoModelForCausalLM, AutoTokenizer,AutoModel
import argparse
import ast
import json
import logging
import numpy as np
import os
import pandas as pd
import pickle
import torch
import traceback
import transformers
import pyarrow as pa
import pyarrow.parquet as pq
from cache_handler import cache_handler
 
from dataloading.semeval_load_dataset import load_phase_dataset
from py_evaluator.test_case_runner import is_predicted_type 
logging.basicConfig(level=logging.INFO)

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from runners.reward_function import error_detecting_reward_fn

def run_pipeline_on_qa_parallel(qa, dataset_map, prompt_dataset,test_dataset, 
                                    model, tokenizer,
                                    cache_dir=None,
                                    use_cache=True,
                                    regenerate=False,
                                    horizon=32, 
                                    rollouts=100, 
                                    num_threads=1, 
                                    start_idx=None, 
                                    end_idx=None):
    start_idx = start_idx if start_idx is not None else 0
    end_idx = end_idx if end_idx is not None else (len(qa)-1)

    if (start_idx < 0 or start_idx >= len(qa)) or (end_idx< 0 or end_idx >= len(qa)):
        print(f"Invalid start_idx or end_idx: {start_idx}, {end_idx}")
        return
    output_list = {}
    process_indices = range(start_idx, end_idx)
    with ThreadPoolExecutor(max_workers=num_threads) as executor:  # Adjust max_workers based on your CPU capacity
        futures = {
            executor.submit(run_pipeline_on_qa_single, 
                            idx, 
                            qa[idx], 
                            dataset_map, 
                            test_dataset, 
                            prompt_dataset, 
                            model, 
                            tokenizer,
                            use_cache=use_cache,
                            cache_dir=cache_dir,
                            regenerate=regenerate,
                            horizon=horizon, 
                            rollouts=rollouts): idx
            for idx in process_indices 
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                output_list[idx] = result
            except Exception as e:
                traceback.print_exc()
                print(f"Error processing QA {idx}: {e}")
    return output_list

def convert_pipeline_output_to_parquet(mcts_output):
    """
    Convert the pipeline output to a Parquet file format.
    This function takes the output of a pipeline, which is expected to be a dictionary
    containing 'texts' and 'rewards', and converts it into a PyArrow Table in Parquet format.
    Args:
        output (dict): A dictionary with two keys:
            - 'texts' (list of str): The list of text data.
            - 'rewards' (list of float): The list of reward values corresponding to the texts.
    Returns:
        pa.Table: A PyArrow Table containing the text and reward data.
    Example:
        output = {
            'texts': ["text1", "text2", "text3"],
            'rewards': [0.1, 0.2, 0.3]
        }
        table = convert_pipeline_output_to_parquet(output)
    """
    data = list(zip(mcts_output['texts'], mcts_output['rewards']))
    return pd.DataFrame(data, columns=['text', 'reward'])
    
def construct_cache_key(args, kwargs):
    """
    Constructs a cache key and file path based on the provided arguments and keyword arguments.
    Args:
        args (tuple): Positional arguments passed to the function.
        kwargs (dict): Keyword arguments passed to the function.
    Returns:
        tuple: A tuple containing the cache file path (str) and the index (int).
    The function extracts the necessary arguments for constructing the cache key:
        - 'qa_item' from kwargs or args[1]
        - 'idx' from kwargs or args[0]
        - 'model' from kwargs or args[5]
    It then constructs the cache file path using the model's name or path and the 'semeval_id' from the 'qa_item'.
    """
    # Extract necessary arguments for cache key
    qa_item = kwargs.get('qa_item') if 'qa_item' in kwargs else args[1]
    idx = kwargs.get('idx') if 'idx' in kwargs else args[0]
    model = kwargs.get('model') if 'model' in kwargs else args[5]
    
    # Construct cache file path
    model_name = model.name_or_path if hasattr(model, 'name_or_path') else str(model)
    cache_file_path = f"pipeline-runs/{model_name}/mct-result-semeval_id-{qa_item['semeval_id']}.parquet"
    
    return cache_file_path, idx

def find_prompt_by_semeval_id(semeval_id, prompt_dataset):
    """
    Find a prompt in the dataset by its SemEval ID.

    Args:
        semeval_id (int or str): The SemEval ID of the prompt to find.
        prompt_dataset (Dataset): The dataset containing prompts, where each prompt is a dictionary with a 'semeval_id' key.

    Returns:
        dict: The prompt dictionary that matches the given SemEval ID.

    Raises:
        IndexError: If no prompt with the given SemEval ID is found.
    """
    return prompt_dataset.filter(lambda x: int(x['semeval_id']) == int(semeval_id))[0]

def find_test_questions_by_semeval_id(semeval_id, test_dataset):
    """
    Find test questions in the dataset by their SemEval ID.

    Args:
        semeval_id (int or str): The SemEval ID to search for.
        test_dataset (Dataset): The dataset containing test questions.

    Returns:
        Dataset: A filtered dataset containing only the test questions with the specified SemEval ID.
    """
    return test_dataset.filter(lambda x: int(x['semeval_id']) == int(semeval_id))

@cache_handler(construct_cache_key=construct_cache_key, 
               to_parquet_func=convert_pipeline_output_to_parquet)
def run_pipeline_on_qa_single(idx: int, 
                              qa_item: dict, 
                              dataset_map: dict, 
                              test_dataset , 
                              prompt_dataset, 
                              model: AutoModelForCausalLM, 
                              tokenizer: AutoTokenizer,
                              horizon=32,
                              rollouts=100,
                              # Used be cache_handler decorator
                              use_cache: bool=True, 
                              regenerate: bool=False,
                              cache_dir: str ="~/.cache"):
    try:
        # BEGIN: Extracting the prompt and test cases for the question
        current_semeval_id = qa_item['semeval_id']
        dataset_id = qa_item['dataset']
        backing_df = dataset_map[dataset_id]

        # Find the prompt for this question
        prompt = find_prompt_by_semeval_id(current_semeval_id, prompt_dataset)
        tests_for_questions = find_test_questions_by_semeval_id(current_semeval_id, test_dataset)

        assert prompt['question'] == qa_item['question']
        prompt_content = prompt['content'] 

        logging.info(f"Question [idx:{idx}: semveal_id:{current_semeval_id}] {prompt['question']} Found {len(tests_for_questions)} tests")
       # END: Extracting the prompt and test cases for the question 
        
        # BEGIN: Run Pipline function  
        pipeline = uct_for_hf_transformer_pipeline(
            model=model,
            tokenizer=tokenizer,
            horizon=horizon,
            reward_func=error_detecting_reward_fn(idx, prompt, backing_df, prompt_content, tests=tests_for_questions),
            uct_args=dict(rollouts=rollouts, gamma=1, width=5, alg='p_uct'),
            model_generation_args=dict(top_k=3, top_p=0.9, do_sample=True, temperature=0.2),
            should_plot_tree=False)

        return pipeline(input_str=prompt_content)
    
    except Exception as e:
        traceback.print_exc()
        print(e)
        return {"error": str(e), 'texts': [], 'rewards': []} 

def model_and_tokenzier(model_name="codellama/CodeLlama-7b-Python-hf"):
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        from transformers import  BitsAndBytesConfig
        # Configure 8-bit quantization
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,  
            device_map="auto",  
            trust_remote_code=True,  
            torch_dtype=torch.float16)
        logging.info("Quantization config: %s", quantization_config)
        # Load the tokenizer and model with quantization
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model     = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=quantization_config,
            device_map="auto",
            trust_remote_code=True,
            torch_dtype=torch.float16)
    else:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForCausalLM.from_pretrained(model_name)
    return model, tokenizer

def parse_arguments():
    """
    Parses command-line arguments for running the QA pipeline in parallel.

    Returns:
        argparse.Namespace: Parsed command-line arguments with the following attributes:
            - base_model (str): Base model used to run tree search (default: 'codellama/CodeLlama-13b-Python-hf').
            - cache_dir (str): Output directory for cache (default: '~/.cache').
            - horizon (int): Horizon value (default: 32).
            - num_threads (int): Number of parallel threads (default: 2).
            - start_idx (int, optional): Start index (default: None).
            - end_idx (int, optional): End index (default: None).
            - rollouts (int): Number of rollouts (default: 100).
    """
    parser = argparse.ArgumentParser(description="Run QA pipeline in parallel")
    parser.add_argument('--prompt-dataset', type=str, default='aakarsh-nair/semeval-2025-task-8-prompts-competition', help='Prompt dataset')
    parser.add_argument('--test-dataset', type=str, default='aakarsh-nair/semeval-2025-task-8-test-cases-competition', help='Test dataset')
    parser.add_argument('--base-model', type=str, default='codellama/CodeLlama-7b-Python-hf', help='Base model used run tree search')
    parser.add_argument('--cache-dir', type=str, default=os.path.expanduser("~/.cache"), help='Output directory')
    parser.add_argument('--horizon', type=int, default=42, help='Horizon')
    parser.add_argument('--num-threads', type=int, default=1, help='Number of parallel threads')
    parser.add_argument('--start-idx', type=int, default=None, help='Start Index')
    parser.add_argument('--end-idx', type=int, default=None, help='End Index')
    parser.add_argument('--rollouts', type=int, default=100, help='Number of rollouts')
    parser.add_argument('--enable-cache-regenrations', action='store_false', help='Enable cache regenerations')
    return parser.parse_args()

def main(args):
    # Download the prompt dataset.
    prompt_dataset = load_dataset(args.prompt_dataset, split='dev')
    # Download the tests dataset.
    test_dataset = load_dataset(args.test_dataset, split='dev')
    
    # Load the competition questions dataset and the backing dataset map.
    questions_dataset, dataset_map = load_phase_dataset(phase="competition", split="dev")
    logging.info(f"Loaded {len(prompt_dataset)} prompts and {len(test_dataset)} test cases") 

    # Read in the model and the tokenier.
    model, tokenizer = model_and_tokenzier(args.base_model) 

    run_pipeline_on_qa_parallel(questions_dataset, dataset_map, prompt_dataset, test_dataset, 
                                model, tokenizer, 
                                cache_dir=args.cache_dir,
                                num_threads=args.num_threads,
                                regenerate=args.enable_cache_regenrations,
                                start_idx=args.start_idx, end_idx=args.end_idx)
    
    
if __name__ == "__main__":
    args = parse_arguments()
    print(args)
    main(args)
 