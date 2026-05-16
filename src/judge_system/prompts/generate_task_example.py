example = """
# Design Specification

## Design Goal
Construct a four-legged dining chair with a backrest designed for wood-based assembly.

## Geometry and Dimensions
Approx. 400.0mm × 400.0mm × 880.0mm.

## Material
Timber

## Manufacturing Method
CNC Milling

## Connection Method (Joint Type)
Mortise & Tenon

## Mechanical Condition
Single-person seating.

## Structural Features
Seat panel; four legs; backrest.

## Special Requirements
Keep assembly split unchanged.

## Planned Component Quantity
6

## Component Names
- Seat panel
- Front left leg
- Rear left leg
- Front right leg
- Rear right leg
- Backrest panel

## Adjustable Parameters
- **width**: 400 (300.0 ~ 700.0 mm). Constrains the extreme values to prevent tipping caused by unbalanced length-to-width ratios.
- **depth**: 400 (300.0 ~ 700.0 mm).
- **seat_height**: 450 (350.0 ~ 520.0 mm). Strictly follows ergonomic standards for single-person seating posture.
- **backrest_height**: 400 (250.0 ~ 600.0 mm). Provides adequate lumbar support without raising the center of gravity too high.
- **leg_thickness**: 40 (20.0 ~ 80.0 mm). Lower limit ensures load-bearing stiffness; upper limit prevents interference and material waste.
- **seat_thickness**: 30 (15.0 ~ 60.0 mm). Must be thick enough to accommodate the insertion depth of the leg tenons.
- **tenon_length**: 13.5 (8.0 ~ 40.0 mm). Determines the bite depth of the physical connections.
- **tenon_offset**: 5 (2.0 ~ 20.0 mm). Controls the setback distance of the tenon relative to the part edge to prevent wood splitting.

## Component Details

**Global Output Requirements**
1. The component must remain an independent geometric body.
2. The exported STEP must remain a closed solid.

**Global Modeling Steps**
1. Build the main profile of the part based on the original script.
2. Complete key features like holes, slots, lofts, or chamfers.
3. Place the part back in its original position within the sample assembly.

---

### 1. Seat Panel
The central hub of the chair.
* **Component Purpose**: Acts as the main load-bearing base and provides localization references and mechanical interfaces (sockets) for the legs and backrest.
* **Assembly Direction**: Fixed base component, positioned at absolute $Z = seat\_height$.
* **Connection & Kinematics**: Mortise & Tenon (Rigid when interference-fitted; potential micro-sliding if loose). Bottom features four 40×40mm rectangular sockets; rear features a long slot for the backrest.

### 2~5. Four Legs (Front Left, Rear Left, Front Right, Rear Right)
The supporting entities of the chair.
* **Component Purpose**: Vertical support. Transfers the seat load to the ground, ensuring anti-overturning stability in the X-Y plane.
* **Assembly Direction**: Inserted upwards along the +Z axis into the seat panel.
* **Connection & Kinematics**: Mortise & Tenon (Rigid when interference-fitted). Top features a tenon of length `tenon_length` that interference-fits into the bottom sockets of the seat panel.

### 6. Backrest Panel
The functional support entity of the chair.
* **Component Purpose**: Vertical guide. Provides back support for human-computer interaction, ensuring structural strength under large torque via a long mortise-and-tenon joint.
* **Assembly Direction**: Pressed downwards along the -Z axis into the seat panel.
* **Connection & Kinematics**: Mortise & Tenon (Rigid when interference-fitted). Bottom features a full-width strip tenon inserted into the dedicated long slot at the rear of the seat panel.

---

## Component Assembly Graph (Textual)
Based on the logical mapping of the 6-component model:

* **Front Left Leg -> Seat Panel** | Joint: Mortise & Tenon | Note: Leg top tenon inserted into seat's front-left socket.
* **Front Right Leg -> Seat Panel** | Joint: Mortise & Tenon | Note: Leg top tenon inserted into seat's front-right socket.
* **Rear Left Leg -> Seat Panel** | Joint: Mortise & Tenon | Note: Leg top tenon inserted into seat's rear-left socket.
* **Rear Right Leg -> Seat Panel** | Joint: Mortise & Tenon | Note: Leg top tenon inserted into seat's rear-right socket.
* **Backrest Panel -> Seat Panel** | Joint: Mortise & Tenon | Note: Backrest bottom strip tenon inserted into seat's rear slot.
* **Seat Panel -> All Components** | Joint: Support Base | Note: Acts as the core hub; all connection sockets generated via boolean cut."""