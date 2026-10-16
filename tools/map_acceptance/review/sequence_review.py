"""Strict auxiliary retry review; no SDK/GIS/browser I/O or new paint policy."""
import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path

RETRYABLE = ("insufficient core coverage", "insufficient contour coverage", "insufficient raw contour coverage",
             "insufficient local contour coverage", "hidden background unstable", "shown image unstable", "outside geometry noise")
PHASES = ["hidden-before", "shown-first", "hidden-after", "shown-again"]

def require(value, message):
    if not value:
        raise ValueError(message)

def strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return all(type(key) is str for key in left) and set(left) == set(right) and all(strict_equal(left[key],right[key]) for key in left)
    if type(left) is list:
        return len(left)==len(right) and all(strict_equal(a,b) for a,b in zip(left,right))
    if type(left) is float:
        return math.isfinite(left) and math.isfinite(right) and left == right
    if type(left) in (str,int,bool,type(None)):
        return left == right
    return False

def positive_number(value, label):
    require(type(value) in (int,float) and math.isfinite(value) and value > 0, "Finite positive non-bool required: "+label)

def validate_initial_label(value):
    require(value == "not_yet_proved", "Initial readiness is not a terminal paint proof")
    return True

def validate_budget(budget):
    require(type(budget) is dict and type(budget.get("timeout_seconds")) is int and budget["timeout_seconds"] == 30, "Original30s timeout changed")
    positive_number(budget.get("deadline_monotonic"),"original deadline")
    require(budget.get("deadline_extended") is False,"Original deadline extended")

def validate_sequence(measurement,sequence,budget,*,expected_kind):
    validate_budget(budget)
    require(type(expected_kind) is str and expected_kind in ("overlay","final"),"Unsupported expected sequence kind")
    require(type(measurement) is dict,"Sequence frame binding must be a native object")
    frames = measurement.get("frames")
    require(type(frames) is list and len(frames)==4 and all(type(frame) is dict for frame in frames)
            and [f.get("phase") for f in frames]==PHASES,"Incomplete or reordered four phases")
    require(type(sequence) is dict and sequence.get("sequence_kind")==expected_kind and sequence.get("paint_readiness")=="pending_oracle","Complete frozen sequence must remain pending_oracle")
    require("error" not in sequence and "failed_phase" not in sequence,"Failed/incomplete sequence journal")
    require(strict_equal(sequence.get("frames"),frames),"Sequence/measurement frames differ by type/key/order")
    require(strict_equal(sequence.get("deadline_monotonic"),budget["deadline_monotonic"]) and sequence.get("deadline_extended") is False,"Sequence original deadline differs")
    for frame in frames:
        diag=frame.get("capture_diagnostics")
        require(type(diag) is dict and diag.get("passed") is True and diag.get("deadline_extended") is False,"Capture failed/renewed")
        require(strict_equal(diag.get("deadline_monotonic"),budget["deadline_monotonic"]),"Capture original deadline differs")
        positive_number(diag.get("deadline_remaining_seconds"),"capture remaining")
        operations=diag.get("operations")
        require(type(operations) is list and bool(operations),"Capture operations missing")
        for operation in operations:
            require(type(operation) is dict and operation.get("status")=="returned","Capture operation failed")
            positive_number(operation.get("remaining_before_seconds"),"operation before")
            positive_number(operation.get("remaining_after_seconds"),"operation after")
