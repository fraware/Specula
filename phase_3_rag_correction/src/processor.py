import os
import json
import yaml
import time
import logging
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    from .config import get_config
    from .llm_client import get_llm_client
    from .rag.basic_retriever import ErrorRetriever
    from .processing.get_errors import tla_sany, add_var_func
    from .action_completion import process_actions, add_var_func as action_add_var_func
except ImportError:
    from config import get_config
    from llm_client import get_llm_client
    from rag.basic_retriever import ErrorRetriever
    from processing.get_errors import tla_sany, add_var_func
    from action_completion import process_actions, add_var_func as action_add_var_func

logger = logging.getLogger(__name__)

class TLAProcessor:
    """TLA+ specification processor"""
    
    def __init__(self, config_path: str = None):
        """Initialize processor
        
        Args:
            config_path: Configuration file path
        """
        self.config = get_config(config_path)
        self.llm_client = get_llm_client(config_path)
        self.write_lock = threading.Lock()
        
        # Get configuration
        self.experiments_config = self.config.get_experiments_config()
        self.paths_config = self.config.get_paths_config()
        
    def load_input_data(self, input_file: str) -> List[Dict[str, Any]]:
        """Load input data
        
        Args:
            input_file: Input file path (YAML format)
            
        Returns:
            Parsed data list
        """
        with open(input_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        # Ensure data format is correct
        if isinstance(data, dict) and 'code' in data:
            return data['code']
        elif isinstance(data, list):
            return data
        else:
            raise ValueError("Input file format is incorrect, should contain 'code' field or be in list format")
    
    def simple_correction(self, input_file: str, output_folder: str) -> Dict[str, Any]:
        """Simple correction mode
        
        Args:
            input_file: Input file path
            output_folder: Output folder path
            
        Returns:
            Processing result statistics
        """
        logger.info("Starting simple correction mode")
        
        # Create output directory
        output_path = Path(output_folder)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Step 1: Process actions from YAML file, generate individual TLA+ files
        logger.info("Processing actions from YAML file...")
        try:
            action_files = process_actions(input_file, str(output_path))
            logger.info(f"Generated {len(action_files)} action files")
            
            # Step 2: Complete each action file (add variables and functions)
            logger.info("Completing action files...")
            completed_files = []
            for action_file in action_files:
                full_path = output_path / action_file
                try:
                    # Use action completion function to add missing variables and functions
                    action_add_var_func(str(full_path))
                    completed_files.append(str(full_path))
                    logger.debug(f"Completed action file: {full_path}")
                except Exception as e:
                    logger.error(f"Error completing action file {full_path}: {e}")
            
            # Step 3: Validate and correct specifications (if needed)
            logger.info("Validating and correcting specifications...")
            results = []
            for i, spec_file in enumerate(completed_files):
                try:
                    # First validate specification
                    validation_result = self._validate_spec(Path(spec_file))
                    
                    result = {
                        'index': i,
                        'output_file': spec_file,
                        'validation': validation_result,
                        'success': validation_result.get('passed', False)
                    }
                    
                    if validation_result['passed']:
                        logger.debug(f"Specification validation passed: {spec_file}")
                    else:
                        logger.info(f"Specification validation failed, attempting correction: {spec_file}")
                        # Additional correction logic can be added here if needed
                    
                    results.append(result)
                        
                except Exception as e:
                    logger.error(f"Error processing specification {spec_file}: {e}")
                    results.append({
                        'index': i,
                        'output_file': spec_file,
                        'validation': {'passed': False, 'output': str(e)},
                        'success': False
                    })
            
            # Generate statistics report
            stats = self._generate_stats(results, output_path)
            logger.info(f"Simple correction completed, processed {len(results)} specification files")
            
            return stats
            
        except Exception as e:
            logger.error(f"Error in simple correction mode: {e}")
            raise
    
    def run_experiments(self, input_file: str, output_folder: str) -> Dict[str, Any]:
        """Run three experiment comparison
        
        Args:
            input_file: Input file path
            output_folder: Output folder path
            
        Returns:
            Experiment result statistics
        """
        logger.info("Starting three experiment comparison")
        
        # Create output directory structure
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        base_output = Path(output_folder)
        
        exp1_dir = base_output / f"experiment1_{timestamp}"
        exp2_dir = base_output / f"experiment2_{timestamp}"
        exp3_dir = base_output / f"experiment3_{timestamp}"
        
        for dir_path in [exp1_dir, exp2_dir, exp3_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)
        
        # Run experiment 1: Baseline test
        logger.info("Running experiment 1: Baseline compilation test")
        failed_files_1 = self._run_experiment1(input_file, exp1_dir)
        
        # Run experiment 2: Baseline correction
        logger.info("Running experiment 2: Baseline correction")
        failed_files_2 = self._run_experiment2(failed_files_1, exp2_dir)
        
        # Run experiment 3: RAG correction
        logger.info("Running experiment 3: RAG correction")
        failed_files_3 = self._run_experiment3(failed_files_2, exp3_dir)
        
        # Generate comparison report
        comparison_report = self._generate_comparison_report(
            exp1_dir, exp2_dir, exp3_dir, base_output
        )
        
        logger.info("Three experiment comparison completed")
        return comparison_report
    
    def _process_single_code(self, code: str, prompt: str, output_path: Path, index: int) -> Optional[Dict[str, Any]]:
        """Process single code block"""
        try:
            # Call LLM to generate specification
            if isinstance(code, list):
                # If it's a code list, need to process in chunks
                response = self._split_code_gen_spec(prompt, code)
            else:
                response = self.llm_client.get_completion(prompt, code)
            
            # Save result
            output_file = output_path / f"spec_{index}.tla"
            with self.write_lock:
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(response)
            
            # Validate generated specification
            validation_result = self._validate_spec(output_file)
            
            return {
                'index': index,
                'output_file': str(output_file),
                'validation': validation_result,
                'success': validation_result.get('passed', False)
            }
            
        except Exception as e:
            logger.error(f"Error processing code block {index}: {e}")
            return None
    
    def _split_code_gen_spec(self, prompt: str, code_list: List[str]) -> str:
        """Process code in chunks to generate specification"""
        result = ""
        for i, code in enumerate(code_list):
            context_prompt = f"Due to token limitations, I will divide this function-to-TLA+ specification conversion task into multiple parts. Now I need to complete part {i+1}, the previously generated content is as follows:"
            full_prompt = context_prompt + result + "code:\n" + code
            result += self.llm_client.get_completion(prompt, full_prompt)
        return result
    
    def _validate_spec(self, spec_file: Path) -> Dict[str, Any]:
        """Validate TLA+ specification"""
        try:
            # Add necessary variables and functions
            add_var_func(str(spec_file))
            
            # Run TLA+ SANY check
            output = tla_sany(str(spec_file))
            
            passed = "Error" not in output and "error" not in output
            
            return {
                'passed': passed,
                'output': output,
                'file': str(spec_file)
            }
        except Exception as e:
            return {
                'passed': False,
                'output': str(e),
                'file': str(spec_file)
            }
    
    def _run_experiment1(self, input_file: str, output_dir: Path) -> List[Tuple[str, str]]:
        """Run Experiment 1: Baseline compilation test"""
        logger.info("Running Experiment 1: Baseline compilation test")
        
        failed_files = []
        results_file = output_dir / "compilation_results.txt"
        log_file = output_dir / "experiment1.log"
        
        # Step 1: Process actions from YAML file to generate individual TLA+ files
        logger.info("Processing actions from YAML file...")
        try:
            # Use the existing action completion functionality
            action_files = process_actions(input_file, str(output_dir))
            logger.info(f"Generated {len(action_files)} action files")
            
            # Step 2: Complete each action file (add variables and functions)
            logger.info("Completing action files...")
            completed_files = []
            for action_file in action_files:
                full_path = output_dir / action_file
                try:
                    # Use the action completion function to add missing variables and functions
                    action_add_var_func(str(full_path))
                    completed_files.append(str(full_path))
                    logger.debug(f"Completed action file: {full_path}")
                except Exception as e:
                    logger.error(f"Error completing action file {full_path}: {e}")
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"Error completing action file {full_path}: {str(e)}\n")
            
            # Step 3: Validate all completed files
            logger.info("Validating completed action files...")
            for file_path in completed_files:
                try:
                    # Run TLA+ SANY validation
                    output = tla_sany(file_path)
                    
                    # Check if compilation passed
                    if "Error" in output or "error" in output:
                        failed_files.append((file_path, output))
                        logger.debug(f"Validation failed for {file_path}")
                    else:
                        logger.debug(f"Validation passed for {file_path}")
                        
                except Exception as e:
                    error_msg = f"Exception during validation: {str(e)}"
                    failed_files.append((file_path, error_msg))
                    logger.error(f"Error validating {file_path}: {e}")
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"Error validating {file_path}: {str(e)}\n")
            
            # Write results summary
            total_files = len(completed_files)
            passed_count = total_files - len(failed_files)
            
            with open(results_file, 'w', encoding='utf-8') as f:
                f.write(f"Compilation Results - Experiment 1\n")
                f.write(f"Total files: {total_files}\n")
                f.write(f"Passed: {passed_count}\n")
                f.write(f"Failed: {len(failed_files)}\n\n")
                
                if failed_files:
                    f.write("Failed files and errors:\n")
                    for file_path, error in failed_files:
                        f.write(f"=== {file_path} ===\n{error}\n\n")
            
            logger.info(f"Experiment 1 completed. {passed_count}/{total_files} files passed compilation")
            
        except Exception as e:
            logger.error(f"Error in experiment 1: {e}")
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"Error in experiment 1: {str(e)}\n")
            raise
        
        return failed_files
    
    def _run_experiment2(self, failed_files: List[Tuple[str, str]], output_dir: Path) -> List[Tuple[str, str]]:
        """Run Experiment 2: Baseline correction"""
        logger.info("Running Experiment 2: Baseline correction")
        
        if not failed_files:
            logger.info("No failed files to correct")
            return []
        
        baseline_prompt_path = self.paths_config.get('prompts', {}).get('baseline')
        with open(baseline_prompt_path, 'r', encoding='utf-8') as f:
            prompt = f.read()
        
        results_file = output_dir / "compilation_results.txt"
        log_file = output_dir / "experiment2.log"
        
        # Process failed files with baseline correction
        max_workers = self.experiments_config.get('max_workers', 5)
        corrected_files = []
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for file_path, error in failed_files:
                future = executor.submit(
                    self._correct_single_file, 
                    file_path, error, output_dir, log_file, prompt
                )
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        corrected_files.append(result)
                except Exception as e:
                    logger.error(f"Error correcting file: {e}")
        
        # Validate corrected files
        still_failed = []
        for corrected_file in corrected_files:
            validation_result = self._validate_spec(Path(corrected_file))
            if not validation_result['passed']:
                still_failed.append((corrected_file, validation_result['output']))
        
        # Write results
        passed_count = len(corrected_files) - len(still_failed)
        with open(results_file, 'w', encoding='utf-8') as f:
            f.write(f"Correction Results - Experiment 2\n")
            f.write(f"Total corrected files: {len(corrected_files)}\n")
            f.write(f"Passed after correction: {passed_count}\n")
            f.write(f"Still failed: {len(still_failed)}\n\n")
            
            if still_failed:
                f.write("Still failed files and errors:\n")
                for file_path, error in still_failed:
                    f.write(f"=== {file_path} ===\n{error}\n\n")
        
        logger.info(f"Experiment 2 completed. {passed_count}/{len(corrected_files)} files passed after correction")
        return still_failed
    
    def _run_experiment3(self, failed_files: List[Tuple[str, str]], output_dir: Path) -> List[Tuple[str, str]]:
        """Run Experiment 3: RAG-enhanced correction"""
        logger.info("Running Experiment 3: RAG-enhanced correction")
        
        if not failed_files:
            logger.info("No failed files to correct")
            return []
        
        # Initialize RAG retriever
        knowledge_base_path = self.paths_config.get('knowledge_base')
        if not knowledge_base_path or not Path(knowledge_base_path).exists():
            raise FileNotFoundError(f"Knowledge base not found: {knowledge_base_path}")
        
        retriever = ErrorRetriever(knowledge_base_path)
        
        rag_prompt_path = self.paths_config.get('prompts', {}).get('rag')
        with open(rag_prompt_path, 'r', encoding='utf-8') as f:
            prompt = f.read()
        
        results_file = output_dir / "compilation_results.txt"
        log_file = output_dir / "experiment3.log"
        
        # Process failed files with RAG correction
        max_workers = self.experiments_config.get('max_workers', 5)
        corrected_files = []
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for file_path, error in failed_files:
                # Retrieve similar errors from knowledge base
                error_info = self._extract_error_info(error)
                similar_errors = retriever.search(error_info, top_k=3)
                example_info = self._format_examples(similar_errors)
                
                future = executor.submit(
                    self._correct_single_file, 
                    file_path, error, output_dir, log_file, prompt, example_info
                )
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        corrected_files.append(result)
                except Exception as e:
                    logger.error(f"Error correcting file: {e}")
        
        # Validate corrected files
        still_failed = []
        for corrected_file in corrected_files:
            validation_result = self._validate_spec(Path(corrected_file))
            if not validation_result['passed']:
                still_failed.append((corrected_file, validation_result['output']))
        
        # Write results
        passed_count = len(corrected_files) - len(still_failed)
        with open(results_file, 'w', encoding='utf-8') as f:
            f.write(f"RAG Correction Results - Experiment 3\n")
            f.write(f"Total corrected files: {len(corrected_files)}\n")
            f.write(f"Passed after RAG correction: {passed_count}\n")
            f.write(f"Still failed: {len(still_failed)}\n\n")
            
            if still_failed:
                f.write("Still failed files and errors:\n")
                for file_path, error in still_failed:
                    f.write(f"=== {file_path} ===\n{error}\n\n")
        
        logger.info(f"Experiment 3 completed. {passed_count}/{len(corrected_files)} files passed after RAG correction")
        return still_failed
    
    def _correct_single_file(self, file_path: str, error: str, output_dir: Path, 
                           log_file: Path, prompt: str, example_info: str = None) -> Optional[str]:
        """Correct a single TLA+ file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
            
            # Prepare correction prompt
            if example_info:
                content = f"error: \n{error}\n\ncode: \n{code}\n\nexample_info: \n{example_info}"
            else:
                content = f"error: \n{error}\n\ncode: \n{code}"
            
            # Get correction from LLM
            response = self.llm_client.get_completion(prompt, content)
            
            # Log the response
            with self.write_lock:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"File {file_path} correction response:\n{response}\n\n")
            
            # Try to parse JSON response
            try:
                import json
                response_json = json.loads(response)
                corrected_spec = response_json.get("spec")
                if not corrected_spec:
                    logger.warning(f"No 'spec' field in response for {file_path}")
                    return None
            except json.JSONDecodeError:
                # If not JSON, treat the entire response as the corrected spec
                corrected_spec = response
            
            # Save corrected file
            corrected_file = output_dir / f"corrected_{Path(file_path).name}"
            with open(corrected_file, 'w', encoding='utf-8') as f:
                f.write(corrected_spec)
            
            return str(corrected_file)
            
        except Exception as e:
            logger.error(f"Error correcting file {file_path}: {e}")
            with self.write_lock:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"Error correcting file {file_path}: {str(e)}\n")
            return None
    
    def _extract_error_info(self, error: str) -> str:
        """Extract key error information for RAG retrieval"""
        # Extract parse errors
        if "***Parse Error***" in error:
            lines = error.split('\n')
            start_idx = -1
            end_idx = -1
            
            for i, line in enumerate(lines):
                if "***Parse Error***" in line:
                    start_idx = i + 1
                if "Residual stack trace follows:" in line:
                    end_idx = i
                    break
                    
            if start_idx != -1 and end_idx != -1:
                return "\n".join(lines[start_idx:end_idx]).strip()
        
        # Extract semantic errors
        elif "*** Errors:" in error:
            lines = error.split('\n')
            start_idx = -1
            
            for i, line in enumerate(lines):
                if "*** Errors:" in line:
                    start_idx = i + 1
                    break
                    
            if start_idx != -1:
                return "\n".join(lines[start_idx:]).strip()
        
        # Return the full error if no specific pattern found
        return error.strip()
    
    def _format_examples(self, similar_errors: List[Dict[str, Any]]) -> str:
        """Format similar errors as examples for the prompt"""
        if not similar_errors:
            return ""
        
        examples = []
        for i, result in enumerate(similar_errors, 1):
            example = f"Solution {i}:\n"
            example += f"Error message: {result.get('error_message', '')}\n"
            example += f"Solution: {result.get('solution', '')}\n"
            example += f"Context: {result.get('context', '')}\n"
            examples.append(example)
        
        return "\n".join(examples)
    
    def _generate_stats(self, results: List[Dict[str, Any]], output_path: Path) -> Dict[str, Any]:
        """Generate statistics report"""
        total = len(results)
        passed = sum(1 for r in results if r.get('success', False))
        failed = total - passed
        
        stats = {
            'total': total,
            'passed': passed,
            'failed': failed,
            'success_rate': passed / total if total > 0 else 0
        }
        
        # Save statistics report
        stats_file = output_path / "stats.json"
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        
        return stats
    
    def _generate_comparison_report(self, exp1_dir: Path, exp2_dir: Path, exp3_dir: Path, output_dir: Path) -> Dict[str, Any]:
        """Generate experiment comparison report"""
        # Implement comparison report generation logic here
        report = {
            'experiment1': {'description': 'Baseline compilation test'},
            'experiment2': {'description': 'Baseline correction'},
            'experiment3': {'description': 'RAG correction'},
            'comparison': {}
        }
        
        # Save comparison report
        report_file = output_dir / "comparison_report.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        return report 