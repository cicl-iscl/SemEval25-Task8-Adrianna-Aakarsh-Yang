from functools import wraps
from typing import Callable, Optional, Any
import os
import pickle
import logging
import traceback
import pandas as pd

def cache_handler(
    construct_cache_key: Optional[Callable[[tuple, dict], str]] = None,
    to_parquet_func: Optional[Callable[[Any], pd.DataFrame]] = None):
    """
    Decorator to handle caching of function results.
    
    Args:
        cache_dir (str): Base directory for caching.
        use_cache (bool): Whether to use caching.
        regenerate (bool): Whether to regenerate results even if cache exists.
        to_parquet_func (Optional[Callable[[Any], pd.DataFrame]]): Function to convert result to a DataFrame for parquet.
    
    Returns:
        Callable: Wrapped function with caching capability.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Optional[Any]]:

        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[Any]:
            # Get use_cache and cache_dir from kwargs if available
            use_cache = kwargs.get('use_cache', True)
            cache_dir = kwargs.get('cache_dir', '~/.cachee')
            regenerate = kwargs.get('regenerate', False)

            cache_file_path, idx = construct_cache_key(args, kwargs)
            output_dir = os.path.expanduser(f"{cache_dir}/{os.path.dirname(cache_file_path)}")
            cache_file_path = os.path.expanduser(f"{cache_dir}/{cache_file_path}")
            
            if use_cache and os.path.exists(cache_file_path) and not regenerate:
                logging.info(f"Skipping cache item {kwargs.get('qa_item', args[1])['semeval_id']} as cache exists.")
                return None
            
            # Ensure the output directory exists
            if not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)
            try:
                # Execute the core function
                result = func(*args, **kwargs)
                
                # Convert the result to a DataFrame using the provided function or default to a single-row DataFrame
                df = to_parquet_func(result) if to_parquet_func else pd.DataFrame([result])
                
                # Save the result to cache using parquet
                df.to_parquet(cache_file_path)
                logging.info(f"Saved result to cache: {cache_file_path}")
                return df
            except Exception as e:
                # Ensure the error log directory exists
                os.makedirs(output_dir, exist_ok=True)
                # Save the error log
                error_log_path = os.path.join(output_dir, f"err-parallel-output_list-{idx}-06-01-2025.log")
                with open(error_log_path, 'w+') as f:
                    f.write(f"Error: {e}\n")
                    traceback.print_exc(file=f)
                
                logging.error(f"Error processing QA {idx}: {e}")
                traceback.print_exc()
                
                return None
        return wrapper
    return decorator