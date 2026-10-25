# Trackmania example

This configuration was confirmed working on Windows with a REALFORCE R3SD11,
firmware 0.12. Left/Right arrow depth controls analog steering through the
bridge's vJoy X axis. Up and Space remain ordinary keyboard inputs.

1. Complete the bridge's calibration and check partial presses using
   **Start preview**.
2. Click **Stop**, then **Start vJoy output**. Leave the utility running.
3. Open Trackmania's control bindings and assign **Steering (analog)** to the
   **vJoy X axis**. Press an arrow while assigning the axis.
4. Remove the ordinary keyboard **Left** and **Right** steering bindings.
   Otherwise those keypresses also request full digital steering.
5. Bind **Up** to acceleration and **Space** to brake.
6. Test gentle arrow presses. They should produce partial steering, with full
   presses reaching full steering. Use Trackmania's own sensitivity and Action
   Keys for driving adjustments.

Click **Stop** in the bridge when finished; it centers and releases the virtual
axis. Calibration remains saved for the next launch.

For setup and driver installation, see the [main README](../README.md).
