import numpy as np
import open3d as o3d
from pathlib import Path
from numba import njit, prange, jit, set_num_threads
import numba
from scipy.spatial import cKDTree
from typing import Tuple, List, Dict
import time
import psutil
import threading
import time
import csv
import os
from datetime import datetime
import pytz
import subprocess
import joblib
from sklearn.metrics import confusion_matrix, classification_report
import gzip
import pandas as pd
import pickle

# Hydra and OmegaConf imports
import hydra
from hydra import compose, initialize
from omegaconf import DictConfig


def import_pcd(file_path: str, delimiter: str = ' ') -> np.ndarray:
    return np.loadtxt(file_path, delimiter=delimiter)


def create_o3d_pcd(pcd: np.ndarray) -> Tuple[o3d.geometry.PointCloud, np.ndarray]:
    # scanner_pos = np.mean(pcd[:,:3], axis=0)
    # pcd[:, :3] -= scanner_pos
    
    o3d_pcd = o3d.geometry.PointCloud()
    o3d_pcd.points = o3d.utility.Vector3dVector(pcd[:, :3])
    
    return o3d_pcd


def subsample_pcd(o3d_pcd: o3d.geometry.PointCloud, 
                  original_pcd: np.ndarray, 
                  voxel_size: float) -> np.ndarray:
    o3d_pcd_subsampled = o3d_pcd.voxel_down_sample(voxel_size)
    
    # Convert to numpy array
    subsampled_pcd = np.asarray(o3d_pcd_subsampled.points)
    #subsampled_pcd[:, :3] += scanner_pos
    
    # Create a KDTree
    tree = cKDTree(original_pcd[:, :3])

    # Query both point clouds
    _, indices = tree.query(subsampled_pcd[:, :3], k=1, workers=6)

    # Filter pcd based on the indices
    original_pcd_subsampled = original_pcd[indices, :]
    
    return original_pcd_subsampled


def center_pcd(pcd: np.ndarray) -> np.ndarray:
    # Compute the centroid of the point cloud
    scanner_pos = np.mean(pcd[:,:3], axis=0)
    pcd_centered = pcd.copy()

    # Center the point cloud
    pcd_centered[:,:3] -= scanner_pos

    return pcd_centered, scanner_pos


@njit()
def compute_roughness(pcd: np.ndarray, normal: np.ndarray, point: np.ndarray) -> np.ndarray:
    # Compute the mean of the neighborhood points along axis 0 manually
    # Approximates the best fit plane to the neighborhood points
    mean_pt_nbh = np.sum(pcd[:,:3], axis=0) / pcd.shape[0]
    
    # Compute the distance from the points to the plane
    return np.abs(np.dot(point - mean_pt_nbh, normal))


def initialize_attributes(cfg, pcd: np.ndarray) -> numba.typed.Dict:
    attributes = numba.typed.Dict.empty(
        key_type=numba.types.unicode_type,
        value_type=numba.types.float64[:]
    )
    
    attributes_list = ["z", "red", "green", "vert_angle", "zenith_angle", "curvature", "roughness", "nx", "ny", "nz"]
    stats_list = ["mean", "std", "perc2nd", "perc25th", "perc75th", "perc98th"]
    
    for attr in attributes_list:
        for stat in stats_list:
            attributes[attr + "_" + stat] = np.zeros(pcd.shape[0])
            
    attributes['label'] = pcd[:, cfg.pcd_col.label]

    # attributes["z"] = np.zeros(pcd.shape[0])
    # attributes["red"] = np.zeros(pcd.shape[0])
    # attributes["green"] = np.zeros(pcd.shape[0])
    # attributes["vert_angle"] = np.zeros(pcd.shape[0])
    # attributes["zenith_angle"] = np.zeros(pcd.shape[0])
    # attributes["curvature"] = np.zeros(pcd.shape[0])
    # attributes["roughness"] = np.zeros(pcd.shape[0])
    # attributes["nx"] = np.zeros(pcd.shape[0])
    # attributes["ny"] = np.zeros(pcd.shape[0])
    # attributes["nz"] = np.zeros(pcd.shape[0])

    return attributes


def columns_for_numba(cfg):
    columns = numba.typed.Dict.empty(
        key_type=numba.types.unicode_type,
        value_type=numba.types.int64
    )

    columns["z"] = cfg.pcd_col.z
    columns["red"] = cfg.pcd_col.red
    columns["green"] = cfg.pcd_col.green
    columns["vert_angle"] = cfg.pcd_col.vert_angle

    return columns

@njit()
def adjust_angles(theta_zf: np.ndarray) -> tuple:
    """
    Adjusts theta and phi values for a Z+F point cloud.

    Args:
        theta_zf (np.ndarray): Array of theta values.
        phi_zf (np.ndarray): Array of phi values.

    Returns:
        tuple: Adjusted theta and phi values.
    """
    # Flip theta values
    theta_adjusted = 360 - theta_zf

    # Get indices of theta values > 180
    theta_adjusted_idx = np.where(theta_adjusted > 180)[0]

    # Flip and shift theta values > 180
    theta_adjusted[theta_adjusted_idx] *= -1
    theta_adjusted[theta_adjusted_idx] += 360

    return theta_adjusted


def compute_scanner_LOS(pcd: np.ndarray):
    scanner_LOS = pcd[:, :3] / np.linalg.norm(pcd[:,:3], axis=1, keepdims=True)
    return -scanner_LOS


@njit(parallel=True)
def compute_covariance_attributes(indices: np.ndarray, 
                                  list_indices: np.ndarray, 
                                  pcd: np.ndarray,
                                  scanner_LOS: np.ndarray) -> numba.typed.Dict:
    # Initialize the arrays
    normals = np.zeros((pcd.shape[0],3))
    roughness = np.zeros(pcd.shape[0])
    curvature = np.zeros(pcd.shape[0])
    zenith_angle = np.zeros(pcd.shape[0])

    # Loop over all indices in parallel
    for i in prange(len(list_indices) - 1):
        start, end = list_indices[i], list_indices[i+1]
        
        # Select the i-th point cloud from point_clouds
        point_cloud = pcd[indices[start:end], :]

        if point_cloud.shape[0] > 2:
            # Compute the covariance matrix of point_cloud and find its eigenvectors
            eigenvalues, eigenvectors = np.linalg.eigh(np.cov(point_cloud[:,:3].T))

            # The first eigenvector (corresponding to the smallest eigenvalue) is the normal of the point cloud
            normal = eigenvectors[:, 0]
            
            # Align the normal with the scanner position
            if np.dot(normal, scanner_LOS[i, :3]) < 0:
                normal *= -1
            
            curvature[i] = np.min(eigenvalues) / np.sum(eigenvalues)
            normals[i, :] = normal
            roughness[i] = compute_roughness(point_cloud, normal, pcd[i, :3])
            zenith_angle[i] = np.degrees(np.arccos(normal[2]))
        else:
            continue     

    return normals, roughness, curvature, zenith_angle 


@njit(parallel=True)
def compute_attributes(attributes_dict: numba.typed.Dict, 
                       columns_dict: numba.typed.Dict, 
                       indices: np.ndarray, 
                       list_indices: np.ndarray, 
                       pcd: np.ndarray,
                       zenith_angle: np.ndarray,
                       roughness: np.ndarray,
                       normals: np.ndarray,
                       curvature: np.ndarray) -> numba.typed.Dict:
    # Loop over all indices in parallel
    for i in prange(len(list_indices) - 1):
        start, end = list_indices[i], list_indices[i+1]
        
        point_cloud = pcd[indices[start:end], :]
        
        attributes_list = ["z", "red", "green", "vert_angle", "zenith_angle", "curvature", "roughness", "nx", "ny", "nz"]

        if point_cloud.shape[0] > 2:
            for attr in attributes_list:
                if attr in columns_dict:
                    values = point_cloud[:, columns_dict[attr]]
                elif attr == "vert_angle":
                    values = adjust_angles(point_cloud[:, columns_dict[attr]])
                elif attr == "curvature":
                    values = curvature[indices[start:end]]
                elif attr == "roughness":
                    values = roughness[indices[start:end]]
                elif attr == "zenith_angle":
                    values = zenith_angle[indices[start:end]]
                elif attr == "nx":
                    values = normals[indices[start:end], 0]
                elif attr == "ny":
                    values = normals[indices[start:end], 1]
                elif attr == "nz":
                    values = normals[indices[start:end], 2]
                else:
                    continue
                    
                attributes_dict[attr + "_mean"][i] = np.nanmean(values)
                attributes_dict[attr + "_std"][i] = np.nanstd(values)
                attributes_dict[attr + "_perc2nd"][i] = np.nanpercentile(values, 2)
                attributes_dict[attr + "_perc25th"][i] = np.nanpercentile(values, 25)
                attributes_dict[attr + "_perc75th"][i] = np.nanpercentile(values, 75)
                attributes_dict[attr + "_perc98th"][i] = np.nanpercentile(values, 98)
        else:
            continue  
    
    return attributes_dict


def compute_kdtree(pcd: np.ndarray, search_radius: float) -> Tuple[np.ndarray, np.ndarray]:
    # Build a k-d tree from point_clouds for efficient nearest neighbor search
    kdtree = cKDTree(pcd[:,:3])

    # Query the k-d tree for the num_nearest_neighbors nearest neighbors of each point in point_clouds
    indices = kdtree.query_ball_point(pcd[:,:3], r=search_radius, workers=6)

    # Flatten the list of lists
    indices_flattened = np.concatenate(indices)
    
    # Compute the start indices of each sublist in the flattened list
    kdtree_lists_breakpoints = np.cumsum([0] + [len(lst) for lst in indices])
    
    return indices_flattened, kdtree_lists_breakpoints
        
        
def track_performance(cfg, output_dir):
    performance_metrics_path = output_dir / "performance_report" / 'performance_metrics.csv'
    performance_metrics_path.parent.mkdir(parents=False, exist_ok=True)
    
    with open(performance_metrics_path, 'w', newline='') as file:
        writer = csv.writer(file, delimiter=',')
        writer.writerow(["Timestamp", "CPU Usage (%)", "Memory Usage (GB)", "Disk Usage (%)", "Network Activity (Bytes sent, Bytes received)"])

        while True:
            berlin_tz = pytz.timezone('Europe/Berlin')
            berlin_time = datetime.now(berlin_tz)
            timestamp = berlin_time.strftime("%Y-%m-%d %H:%M:%S")
            cpu_usage = psutil.cpu_percent(interval=1)
            memory_usage_gb = psutil.Process().memory_info().rss / (1024 ** 3)
            disk_usage = psutil.disk_usage('/').percent
            net_io = psutil.net_io_counters()
            network_activity = (net_io.bytes_sent, net_io.bytes_recv)

            print(f'Timestamp: {timestamp}, CPU Usage: {cpu_usage}%, Memory Usage: {memory_usage_gb:.3f} GB, Disk Usage: {disk_usage}%, Network Activity: {network_activity}')
            writer.writerow([timestamp, cpu_usage, memory_usage_gb, disk_usage, network_activity])

            file.flush()  # Flush the file buffer
            os.fsync(file.fileno())  # Ensure it's written to disk
            
            time.sleep(0.01)


def evaluate_classifier(rf_model, features, y_test):
    y_pred = rf_model.predict(features)
    
     # Get the confusion matrix
    cnf_matrix = confusion_matrix(y_test, y_pred)
    
    # Get the classification report
    cls_report = classification_report(y_test, y_pred, digits=3, target_names=np.array(["unclassified", "man-made objects", "ground", "tree trunk/branches", "leaves", "low vegetation"]), output_dict=True)

    return y_pred, cnf_matrix, cls_report


def classification(cfg: DictConfig,
                   pcd_subsampled: np.ndarray,
                   attributes_dict: dict, 
                   model_filepath: str,
                   output_dir):
    
    # Get features and ground truth labels
    attributes_arr = np.array(list(attributes_dict.values())).T
    features = attributes_arr[:, :-1]
    labels = attributes_arr[:, -1]
    
    # Load the model
    rf_model = joblib.load(model_filepath)
    
    # Prediction and evaluation
    y_pred, cnf_matrix, cls_report = evaluate_classifier(rf_model, features, labels)
    
    # Save confusion matrix
    filename = Path(cfg.cls_3d.input_file_path).stem
    cnf_out = output_dir / "confusion_matrix"
    cnf_out.mkdir(parents=False, exist_ok=True)
    np.savetxt(cnf_out / f'{filename}_cnf_matrix.txt', cnf_matrix, delimiter=',', fmt='%u')
    
    # Save classification report
    cls_report_df = pd.DataFrame(cls_report).transpose()
    cls_report_df['filename'] = Path(cfg.cls_3d.input_file_path).stem
    classification_report_dir = output_dir / "classification_report"
    classification_report_dir.mkdir(parents=False, exist_ok=True)
    cls_report_df.to_csv(classification_report_dir / f"{filename}_cls_report.csv")
    
    # Output pcd with labels and classification result
    pcd_subsampled_classified = np.c_[pcd_subsampled[:, :3], labels, y_pred]
    
    if cfg.save_cls_result:
        cls_out = output_dir / "classified_pcd"
        cls_out.mkdir(parents=False, exist_ok=True)
        
        fmt = "%1.4f %1.4f %1.4f %u %u"
        np.savetxt(cls_out / f"pcd_{filename}_classified.txt", pcd_subsampled_classified, delimiter=' ', fmt=fmt)
    
    return pcd_subsampled_classified
    


# def track_energy(cfg: DictConfig):
#     energy_metrics_path = Path(cfg.cls_3d.output_dir) / 'energy_metrics.txt'
    
#     with open(energy_metrics_path, 'w') as file:
#         # Add that the command needs to be stopped after 60 seconds
#         #process = subprocess.Popen(['sudo', 'powermetrics', '--samplers', 'all',  '--hide-cpu-duty-cycle', '--show-usage-summary'], stdout=file)
#         time.sleep(60)  # Collect data for 60 seconds
#         #process.terminate()


@hydra.main(version_base=None, config_path="../../../config", config_name="main")
def main(cfg: DictConfig):
    start_time = time.time()
    output_dir = Path(cfg.cls_3d.output_dir) / f"radius{int(cfg.cls_3d.nghb_search_radius*100)}cm_voxel{int(cfg.cls_3d.voxel_size*100)}cm"
    output_dir.mkdir(parents=False, exist_ok=True)
    
    # Start tracking memory usage
    #threading.Thread(target=track_memory, daemon=True).start()
    threading.Thread(target=track_performance, args=(cfg,output_dir,), daemon=True).start()
    #threading.Thread(target=track_energy, args=(cfg,), daemon=True).start()
    
    # Clear the hydra config cache
    hydra.core.global_hydra.GlobalHydra.instance().clear()
    
    print("Importing the point cloud")
    pcd = import_pcd(cfg.cls_3d.input_file_path)
    o3d_pcd = create_o3d_pcd(pcd)
    pcd_subsampled = subsample_pcd(o3d_pcd, pcd, cfg.cls_3d.voxel_size)
    pcd_centered, scanner_pos = center_pcd(pcd_subsampled)
    
    print(f"Compute kdtree")
    indices_flattened, kdtree_lists_breakpoints = compute_kdtree(pcd_centered, cfg.cls_3d.nghb_search_radius)
    scanner_LOS = compute_scanner_LOS(pcd_centered)
    attributes = initialize_attributes(cfg, pcd_centered)
    columns = columns_for_numba(cfg)
    normals, roughness, curvature, zenith_angle = compute_covariance_attributes(indices_flattened, kdtree_lists_breakpoints, pcd_centered, scanner_LOS)
    attributes_dict = compute_attributes(attributes, columns, indices_flattened, kdtree_lists_breakpoints, pcd_centered, zenith_angle, roughness, normals, curvature)
    
    # Save the normals and roughness to a file along with xyz
    attributes_arr = np.array(list(attributes_dict.values())).T
    attributes_arr = np.c_[pcd_subsampled[:,:3], attributes_arr]
    print(attributes_arr.shape)
    
    column_names = list(attributes_dict.keys())
    print(column_names)
    print(len(column_names))
    column_names = ["x", "y", "z"] + column_names
    print(len(column_names))
    
    column_names_dir = output_dir / "column_names"
    column_names_dir.mkdir(parents=False, exist_ok=True)
    
    # Save the column names to a oickle file 
    with open(column_names_dir / "column_names.pkl", 'wb') as f:
        pickle.dump(column_names, f)
    
    filename = Path(cfg.cls_3d.input_file_path).stem
    attributes_dir = output_dir / "attributes"
    attributes_dir.mkdir(parents=True, exist_ok=True)
    
    attributes_out = gzip.GzipFile(attributes_dir / f'{filename}_attributes_searchr{cfg.cls_3d.nghb_search_radius}_voxsize{cfg.cls_3d.voxel_size}_rows{attributes_arr.shape[0]}_cols{attributes_arr.shape[1]}.npy.gz', "w")
    np.save(file=attributes_out, arr=attributes_arr)
    attributes_out.close()
    
    if cfg.cls_3d.classification:
        pcd_subsampled_classified = classification(cfg=cfg, pcd_subsampled=pcd_subsampled, 
                                                   attributes_dict=attributes_dict, 
                                                   model_filepath=cfg.cls_3d.model_path,
                                                   output_dir=output_dir)
    
    # pcd_out = np.c_[pcd_centered[:,:3], attributes_dict["nx_mean"], attributes_dict["ny_mean"], attributes_dict["nz_mean"], attributes_dict["zenith_angle_mean"], attributes_dict["roughness_mean"], attributes_dict["curvature_mean"], attributes_dict["z_mean"], attributes_dict["red_mean"], attributes_dict["green_mean"], attributes_dict["vert_angle_mean"], attributes_dict['label']]
    # np.savetxt(Path(cfg.cls_3d.output_dir) / '3d_cls.txt', pcd_out, delimiter=' ', fmt='%1.6f')

    end_time = time.time()
    execution_time = end_time - start_time
    print("Execution time of main is: ", execution_time, "seconds")

if __name__=='__main__':
    set_num_threads(int(5))
    main()