import json
import argparse 
import logging
import os
import pandas as pd

from multiprocessing import Pool, Manager, cpu_count
from utils.dir_utils import (find_all_output_files, collect_all_results)

from dataloading.semeval_load_dataset import load_phase_dataset
from runners.push_results_to_finetune import parse_return_statement 
from py_evaluator.utils import evaluate_return_statement

logging.basicConfig(level=logging.DEBUG)

def find_parquet_files(root_dir, model_name):
    """
    Find all the parquet files in the given directory.
    
    Args:
        root_dir (str): The path to the directory containing the parquet files.
        model_name (str): The name of the model.
        
    Returns:
        list: A list of paths to the parquet files.
    """
    output_files =  find_all_output_files(f"{root_dir}/{model_name}", ends_with=".parquet")
    logging.debug(f"Found {len(output_files)} parquet files.")
    return output_files

def read_reward_files(root_dir, model_name):
    """
    Parse rewards from the output of the reward function.
    """  
    logging.debug(f"parse_rewards: Root directory: {root_dir}") 
    results = collect_all_results(f"{root_dir}/{model_name}", 
                                                ends_with=".parquet", 
                                                pattern=r".*-(\d+).*")
    parsed_rewards = {
        problem_index: pd.read_parquet(file_path) 
            for problem_index, file_path in results.items()
    }
    return parsed_rewards

def parse_reward_data(reward_data):
    """
    Parse the return statement from the text.
    """
    parsed_reward_data = {}
    for i in reward_data.keys():
        reward_completions = reward_data[i].to_dict() 
        reward_values = reward_completions['reward']
        return_statements = [ parse_return_statement(text) 
                                for a, text in reward_completions['text'].items() ]
        for j in range(len(reward_values)):
            if i not in parsed_reward_data:
                parsed_reward_data[i] = []
            parsed_reward_data[i].append({
                'reward': reward_values[j],
                'return': return_statements[j]
            })
    return parsed_reward_data

def reshape_and_enrich_data(parsed_rewards, questions_dataset, datasets_map):
    """
    Reshape the data and enrich it with additional information.
    """
    parsed_rewards = parsed_rewards.copy()

    for semeval_id in parsed_rewards.keys():
        semeval_id = int(semeval_id)
        question = questions_dataset[semeval_id] 
        parsed_rewards[semeval_id] = {
            'question': question['question'],
            'semeval_id': semeval_id,
            'dataset': question['dataset'],
            'parsed_rewards': parsed_rewards[semeval_id]
        }
    return parsed_rewards


def process_semeval_id(semeval_id, shared_parsed_rewards, question_dataset, datasets_map, datasets_map_lite):
    """
    Process a single semeval_id for parallelization.
    """
    logging.debug(f"Processing semeval_id: {semeval_id}")
    parsed_rewards = shared_parsed_rewards[semeval_id]
    rollout_rewards = parsed_rewards['parsed_rewards']

    for idx, rollout in enumerate(rollout_rewards):
        return_statement = rollout['return']
        reward_received = rollout['reward'] 
        eval_result = evaluate_return_statement(return_statement, datasets_map[question_dataset[semeval_id]['dataset']])
        rollout['eval_result'] = \
            str(eval_result)
        eval_result_lite = \
            evaluate_return_statement(return_statement, datasets_map_lite[question_dataset[semeval_id]['dataset']])
        rollout['eval_result_lite'] = str(eval_result_lite)

        logging.debug(f"eval-result: {semeval_id}-{idx}: all:{eval_result} lite:{eval_result_lite}")
        logging.debug(f"Enriched parsed_rewards: {json.dumps(rollout, indent=4)}")

    # DO-NOT REMOVE: REQUIRED BECAUSE OF THE SHARED MEMORY SYNCHRONIZATION
    shared_parsed_rewards[semeval_id] = parsed_rewards
    
    logging.debug(f" ENRICHED_DATA: {json.dumps(shared_parsed_rewards[semeval_id], indent=4)}")
    return semeval_id, shared_parsed_rewards[semeval_id]


def enrich_with_executions_results(parsed_rewards, question_dataset, datasets_map, datasets_map_lite):
    """
    Enrich the data with the results of the executions.
    """
    semeval_id_list = sorted(map(int, parsed_rewards.keys()))
    # map all parsed rewards keys so they are integers not strings
    #parsed_rewards = { int(k): v for k, v in parsed_rewards.items() }
    with Manager() as manager:
        shared_parsed_rewards = manager.dict(parsed_rewards)
        logging.debug(f"Shared parsed rewards keys: {shared_parsed_rewards.keys()}")
        with Pool(cpu_count()) as pool:
            results = pool.starmap(
                process_semeval_id, 
                [(semeval_id, shared_parsed_rewards, question_dataset, datasets_map, datasets_map_lite) 
                 for semeval_id in semeval_id_list]
            )
        for semeval_id, enriched_data in results:
            parsed_rewards[semeval_id] = enriched_data
    return parsed_rewards

def generate_predictions_files(parsed_rewards, output_dir):
    """
    Generate the predictions by simply predicting the 
    most frequent response for all runs for eval and lite eval, 
    more sophisticated methods can be used to generate predictions.
    Which take into account semantic similarity and max rewards.
    """
    predictions = {}
    for semeval_id in parsed_rewards.keys():
        #semeval_id = int(semeval_id)
        logging.debug(f"Generating predictions for semeval_id: {semeval_id}")
        # filter to keep only eval_results for higest rewards
        records_highest_rewards = [ r for r in parsed_rewards[semeval_id]['parsed_rewards'] if r['reward'] == max([ r['reward'] for r in parsed_rewards[semeval_id]['parsed_rewards'] ]) ] 
        
        eval_results = [ r['eval_result'] for r in records_highest_rewards ]
        eval_results_lite = [ r['eval_result_lite'] for r in records_highest_rewards ]

        selected_records = [ r 
                            for r in records_highest_rewards 
                                if r['eval_result'] == max(set(eval_results), key=eval_results.count) 
                                or r['eval_result_lite'] == max(set(eval_results_lite), key=eval_results_lite.count) ] 
       
        selected_eval_value = selected_records[0]['eval_result']
        selected_eval_lite_value = selected_records[0]['eval_result_lite'] 
        code_return = selected_records[0]['return']

        predictions[int(semeval_id)] = {
            'eval': selected_eval_value, #max(set(eval_results), key=eval_results.count),
            'eval_lite': selected_eval_lite_value, #max(set(eval_results_lite), key=eval_results_lite.count),
            'code': code_return
        }
    return predictions

def generate_submission_from_predictions(num_questions, predictions, output_dir, tag="", missing_submission="MISSING_SUBMISSION"):
    """
   Submissions are ordered by semeval_id, when we don't have a result we use 
   the token MISSING_SUBMISSION. 
   We will create two separate files, one for eval and one for eval_lite.
   called predictions.txt and predictions_lite.txt respectively. 
    """
    sorted_prediction_ids = set(sorted(map(int, predictions.keys())))
    max_id = max(sorted_prediction_ids)
    logging.debug(f"Generating predictions files for max_id{max_id}, number of questions: {num_questions} ids.")

    def single_line_output(prog_output):
        """
        Force program output to be single line.
        """
        # Replace escaped newline characters with actual newlines
        prog_output = prog_output.replace("\\r\\n", "\r\n").replace("\\r", "\r").replace("\\n", "\n")
    
        # Replace different types of newline characters with a space
        prog_output = prog_output.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        # Optionally strip leading and trailing whitespace
        prog_output = prog_output.strip()
        return prog_output
 
    with open(f"{output_dir}/predictions{tag}.txt", "w+") as f:
        for idx in range(0, num_questions): 
            if idx in sorted_prediction_ids:
                f.write(f"{single_line_output(predictions[idx]['eval'])}\n")
            else:
                f.write(f"{missing_submission}\n")

    with open(f"{output_dir}/predictions_lite{tag}.txt", "w+") as f:
        for idx in range(0, num_questions): # 
            if idx in sorted_prediction_ids:
                f.write(f"{single_line_output(predictions[idx]['eval_lite'])}\n")
            else:
                f.write(f"{missing_submission}\n")

    logging.debug(f"Generated predictions files at : {output_dir}")
           
if __name__ == "__main__":
    root_dir = os.path.expanduser("~/.cache/pipeline-runs")

    parser = argparse.ArgumentParser(description="Parse rewards from the output of the reward function.")
    parser.add_argument("--root-dir", type=str, default=root_dir, help="The path to the directory containing the parquet files.")
    parser.add_argument("--base-model", type=str, default="codellama/CodeLlama-7b-Python-hf", help="The name of the repository.")
    parser.add_argument("--phase", type=str, default="competition", help="The phase of the competition.")
    parser.add_argument("--split", type=str, default="dev", help="The dataset split.")

    # parser.add_argument
    args = parser.parse_args()
    questions_dataset, datasets_map = load_phase_dataset(phase=args.phase, split=args.split)
    num_questions = len(questions_dataset)
    logging.debug(f"Number of questions: {len(questions_dataset)}")

    if not os.path.exists("parsed_rewards.json"):
        _, lite_datasets_map = load_phase_dataset(phase=args.phase, split=args.split, lite=True)
        reward_data = read_reward_files(args.root_dir, args.base_model)

        parsed_rewards = parse_reward_data(reward_data)
        parsed_rewards = reshape_and_enrich_data(parsed_rewards, 
                                                questions_dataset, 
                                                datasets_map)

        parsed_rewards = \
            enrich_with_executions_results(parsed_rewards, questions_dataset, datasets_map, lite_datasets_map)

        with open("parsed_rewards.json", "w") as f:
            f.write(json.dumps(parsed_rewards, indent=4))

    
    # TODO: Correlation statistics between reward and answers. 
    # We can use this to determine if the reward function is working as expected.
    
    # TODO: Semantic Equivalence scores between codes on random subsets of data, we can use this 
    # to determine degree of uncertainty reload the parsed rewards.
    
    with open("parsed_rewards.json", "r") as f:
        parsed_rewards = json.load(f)
    predictions = generate_predictions_files(parsed_rewards, output_dir="predictions.json")
    
    # Evaluate the return statements
    with open("predictions.json", "w") as f:
        f.write(json.dumps(predictions, sort_keys=True, indent=4))

    generate_submission_from_predictions(num_questions, predictions, output_dir="../submissions", tag="_latest")
