import os
from pathlib import Path
from datasets import load_dataset

def download_generatd_tests_cases(output_root, split="dev"):
    # Loading the dataset
    test_cases = load_dataset("aakarsh-nair/semeval-2025-task-8-test-cases", split=split)

    # Loop through each test case
    for idx, test_case in enumerate(test_cases):

        model = test_case['model']
        test_case_id = test_case['id']  

        # Create output directory using the model name
        output_dir = os.path.join(output_root, "test_cases", split, *model.split("/"))
        
        # Ensure the directory exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        # Write the test case content to a file
        with open(f"{output_dir}/test_case_{test_case_id}.py", "w") as f:
            f.write(test_case['content'])

