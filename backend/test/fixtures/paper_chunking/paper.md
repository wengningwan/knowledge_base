# Synthetic adsorption study

This document contains invented values solely for software verification.

## 1. Experimental methods

### 1.1. Sample preparation

Sample SX-1 was treated at 300 °C for 2 h under nitrogen.

Sample SX-2 was treated at 400 °C for 1 h under argon.

### 1.2. Uptake model

The uptake follows equation (1):

$$
q = q_m b P / (1 + b P) \tag{1}
$$

In eq 1, q is uptake in mmol/g, q_m is saturation uptake in mmol/g, b is affinity in bar^-1, and P is pressure in bar.

The dimensionless pressure is defined as:

$$
x = P / P_0 \tag{2}
$$

In eq 2, P_0 is the reference pressure in bar.

## 2. Results

Sample SX-1 reached an uptake of 2.4 mmol/g measured at 298 K and 1 bar.

Sample SX-2 reached an uptake of 1.8 mmol/g measured at 298 K and 1 bar.

![Breakthrough curves](figure.png)

Figure 1. Breakthrough curves for SX-1 at 298 K and a flow rate of 20 mL/min.

Table 1. Uptake measured at 298 K and 1 bar.

| Sample | Uptake (mmol/g) |
| --- | --- |
| SX-1 | 2.4 |
| SX-2 | 1.8 |

## References

1. Synthetic reference reserved for software tests.
