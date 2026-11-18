/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    main.c
  * @brief   Discovery BME280 measurements over USB CDC
  ******************************************************************************
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * Licensed under the LICENSE file in this software component.
  * If no LICENSE file is supplied, this software is provided AS-IS.
  ******************************************************************************
  */
/* USER CODE END Header */

#include "main.h"
#include "usb_device.h"
#include "usbd_cdc_if.h"
#include "bme280.h"

#include <stdio.h>
#include <string.h>
#include <math.h>

#ifndef BME280_DOUBLE_ENABLE
#error "Use the Bosch driver's default double-precision configuration."
#endif

#define LED_GREEN   GPIO_PIN_12
#define LED_ORANGE  GPIO_PIN_13
#define LED_RED     GPIO_PIN_14
#define LED_BLUE    GPIO_PIN_15
#define LED_ALL     (LED_GREEN | LED_ORANGE | LED_RED | LED_BLUE)

I2C_HandleTypeDef hi2c1;

/* Retained for compatibility with generated project files. */
I2S_HandleTypeDef hi2s3;
SPI_HandleTypeDef hspi1;

extern USBD_HandleTypeDef hUsbDeviceFS;

/* Debugger variables */
volatile double temperature_c = 0.0;
volatile double humidity_percent = 0.0;
volatile double pressure_hpa = 0.0;

volatile uint32_t sample_count = 0U;
volatile uint32_t attempt_count = 0U;
volatile uint32_t error_count = 0U;
volatile uint32_t measurement_valid = 0U;
volatile uint32_t last_sample_ms = 0U;

volatile uint32_t bme280_address = 0x77U;
volatile uint32_t bme280_found = 0U;
volatile uint32_t sensor_chip_id = 0U;

volatile int32_t driver_result = 0;
volatile uint32_t last_i2c_error = 0U;

volatile uint32_t usb_records_queued = 0U;
volatile uint32_t usb_records_skipped = 0U;

static uint8_t sensor_address = 0x77U;
static uint8_t sensor_ready = 0U;
static struct bme280_dev sensor;

/*
 * USB transfers are asynchronous.
 * This buffer must remain valid until transmission completes.
 */
static uint8_t usb_tx_buffer[320];

/* USB IRQ produces bytes; main loop consumes them. Overflow drops the stream
 * through a newline, never executing a truncated command. */
static volatile uint8_t command_rx[256];
static volatile uint16_t command_head, command_tail;
static volatile uint8_t command_overflow;
static char command_line[80];
static unsigned command_length;
static uint8_t command_dropping;
static char command_ack[240];
static uint16_t command_ack_length;
static char last_command_id[33];
static unsigned last_command_value;
static uint8_t blue_command_state;

void TwinTrust_USB_Receive(const uint8_t *data, uint32_t length)
{
    for (uint32_t i = 0; i < length; ++i) {
        uint16_t next = (uint16_t)((command_head + 1U) % 256U);
        if (command_overflow || next == command_tail) {
            command_overflow = 1U;
            continue;
        }
        command_rx[command_head] = data[i];
        __DMB();
        command_head = next;
    }
}

static uint8_t Command_TrySend(void)
{
    uint8_t sent = 0U;
    uint32_t mask = __get_PRIMASK();
    __disable_irq();
    if (hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED &&
        hUsbDeviceFS.pClassData != NULL) {
        USBD_CDC_HandleTypeDef *cdc = hUsbDeviceFS.pClassData;
        if (cdc->TxState == 0U) {
            memcpy(usb_tx_buffer, command_ack, command_ack_length);
            sent = CDC_Transmit_FS(usb_tx_buffer, command_ack_length) == USBD_OK;
        }
    }
    __set_PRIMASK(mask);
    return sent;
}

static void Command_Execute(void)
{
    /* Strict wire format: TT1 <32 lowercase hexadecimal ID> LED <0|1> */
    if (command_length != 42U || memcmp(command_line, "TT1 ", 4U) ||
        memcmp(command_line + 36U, " LED ", 5U) ||
        (command_line[41] != '0' && command_line[41] != '1')) return;
    char id[33];
    memcpy(id, command_line + 4U, 32U);
    id[32] = 0;
    for (unsigned i = 0; i < 32U; ++i)
        if (!((id[i] >= '0' && id[i] <= '9') ||
              (id[i] >= 'a' && id[i] <= 'f'))) return;
    unsigned value = (unsigned)(command_line[41] - '0');
    const char *status = "applied";
    if (!strcmp(id, last_command_id) && value != last_command_value) {
        status = "id_conflict";
    } else {
        HAL_GPIO_WritePin(GPIOD, LED_BLUE, value ? GPIO_PIN_SET : GPIO_PIN_RESET);
        blue_command_state = (uint8_t)value;
        memcpy(last_command_id, id, sizeof(id));
        last_command_value = value;
    }
    int n = snprintf(command_ack, sizeof(command_ack),
        "{\"device\":\"discovery01\",\"type\":\"command_ack\","
        "\"command_id\":\"%s\",\"status\":\"%s\",\"led\":%u,"
        "\"uptime_ms\":%lu}\r\n", id, status,
        (unsigned)blue_command_state, (unsigned long)HAL_GetTick());
    if (n > 0 && (size_t)n < sizeof(command_ack)) command_ack_length = (uint16_t)n;
}

static void Command_Poll(void)
{
    if (command_ack_length) {
        if (Command_TrySend()) command_ack_length = 0U;
        else return;
    }
    uint32_t mask = __get_PRIMASK();
    __disable_irq();
    if (command_overflow) {
        command_tail = command_head;
        command_overflow = 0U;
        command_length = 0U;
        command_dropping = 1U;
    }
    __set_PRIMASK(mask);
    while (command_tail != command_head && !command_ack_length) {
        uint8_t ch = command_rx[command_tail];
        __DMB();
        command_tail = (uint16_t)((command_tail + 1U) % 256U);
        if (ch == '\n') {
            if (!command_dropping) Command_Execute();
            command_length = 0U;
            command_dropping = 0U;
        } else if (ch != '\r' && !command_dropping) {
            if (command_length < sizeof(command_line)) command_line[command_length++] = (char)ch;
            else { command_dropping = 1U; command_length = 0U; }
        }
    }
    if (command_ack_length && Command_TrySend()) command_ack_length = 0U;
}

void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_I2C1_Init(void);

static BME280_INTF_RET_TYPE Sensor_Read(
    uint8_t reg_addr,
    uint8_t *reg_data,
    uint32_t len,
    void *intf_ptr
);

static BME280_INTF_RET_TYPE Sensor_Write(
    uint8_t reg_addr,
    const uint8_t *reg_data,
    uint32_t len,
    void *intf_ptr
);

static void Sensor_DelayUs(uint32_t period, void *intf_ptr);
static int8_t Sensor_Init(void);
static int8_t Sensor_Measure(void);
static void USB_SendRecord(void);

static BME280_INTF_RET_TYPE Sensor_Read(
    uint8_t reg_addr,
    uint8_t *reg_data,
    uint32_t len,
    void *intf_ptr
)
{
    if (intf_ptr == NULL || len > 65535U)
    {
        return (BME280_INTF_RET_TYPE)-1;
    }

    uint8_t address = *(uint8_t *)intf_ptr;

    HAL_StatusTypeDef status = HAL_I2C_Mem_Read(
        &hi2c1,
        (uint16_t)(address << 1U),
        reg_addr,
        I2C_MEMADD_SIZE_8BIT,
        reg_data,
        (uint16_t)len,
        200U
    );

    last_i2c_error = HAL_I2C_GetError(&hi2c1);

    return status == HAL_OK
        ? BME280_INTF_RET_SUCCESS
        : (BME280_INTF_RET_TYPE)-1;
}

static BME280_INTF_RET_TYPE Sensor_Write(
    uint8_t reg_addr,
    const uint8_t *reg_data,
    uint32_t len,
    void *intf_ptr
)
{
    if (intf_ptr == NULL || len > 65535U)
    {
        return (BME280_INTF_RET_TYPE)-1;
    }

    uint8_t address = *(uint8_t *)intf_ptr;

    HAL_StatusTypeDef status = HAL_I2C_Mem_Write(
        &hi2c1,
        (uint16_t)(address << 1U),
        reg_addr,
        I2C_MEMADD_SIZE_8BIT,
        (uint8_t *)reg_data,
        (uint16_t)len,
        200U
    );

    last_i2c_error = HAL_I2C_GetError(&hi2c1);

    return status == HAL_OK
        ? BME280_INTF_RET_SUCCESS
        : (BME280_INTF_RET_TYPE)-1;
}

static void Sensor_DelayUs(uint32_t period, void *intf_ptr)
{
    (void)intf_ptr;

    if (period != 0U)
    {
        HAL_Delay(
            period / 1000U +
            ((period % 1000U) != 0U ? 1U : 0U) +
            1U
        );
    }
}

static int8_t Sensor_Init(void)
{
    if (HAL_I2C_DeInit(&hi2c1) != HAL_OK)
    {
        Error_Handler();
    }

    __HAL_RCC_I2C1_FORCE_RESET();
    HAL_Delay(1U);
    __HAL_RCC_I2C1_RELEASE_RESET();

    MX_I2C1_Init();
    HAL_Delay(10U);

    memset(&sensor, 0, sizeof(sensor));

    sensor.intf = BME280_I2C_INTF;
    sensor.intf_ptr = &sensor_address;
    sensor.read = Sensor_Read;
    sensor.write = Sensor_Write;
    sensor.delay_us = Sensor_DelayUs;

    bme280_found = 0U;
    sensor_chip_id = 0U;

    int8_t result = bme280_init(&sensor);

    if (result != BME280_OK)
    {
        return result;
    }

    bme280_found = 1U;
    sensor_chip_id = sensor.chip_id;

    struct bme280_settings settings = {0};

    settings.osr_t = BME280_OVERSAMPLING_1X;
    settings.osr_p = BME280_OVERSAMPLING_1X;
    settings.osr_h = BME280_OVERSAMPLING_1X;
    settings.filter = BME280_FILTER_COEFF_OFF;

    uint8_t selected =
        BME280_SEL_OSR_TEMP |
        BME280_SEL_OSR_PRESS |
        BME280_SEL_OSR_HUM |
        BME280_SEL_FILTER;

    return bme280_set_sensor_settings(
        selected,
        &settings,
        &sensor
    );
}

static int8_t Sensor_Measure(void)
{
    struct bme280_data data = {0};

    int8_t result = bme280_set_sensor_mode(
        BME280_POWERMODE_FORCED,
        &sensor
    );

    if (result != BME280_OK)
    {
        return result;
    }

    HAL_Delay(100U);

    result = bme280_get_sensor_data(
        BME280_ALL,
        &data,
        &sensor
    );

    if (result != BME280_OK)
    {
        return result;
    }

    /*
     * Reject non-finite or out-of-range values before
     * converting them to integers for USB formatting.
     */
    if (!isfinite(data.temperature) ||
        !isfinite(data.humidity) ||
        !isfinite(data.pressure) ||
        data.temperature < -40.0 ||
        data.temperature > 85.0 ||
        data.humidity < 0.0 ||
        data.humidity > 100.0 ||
        data.pressure < 30000.0 ||
        data.pressure > 110000.0)
    {
        return (int8_t)-100;
    }

    temperature_c = data.temperature;
    humidity_percent = data.humidity;
    pressure_hpa = data.pressure / 100.0;

    last_sample_ms = HAL_GetTick();
    sample_count++;
    measurement_valid = 1U;

    return BME280_OK;
}

static void USB_SendRecord(void)
{
    char line[320];
    int length;

    if (measurement_valid != 0U)
    {
        /*
         * Format two decimal places using integers.
         * No floating-point printf setting is required.
         */
        double t = temperature_c;

        long t100 = (long)(
            t * 100.0 + (t >= 0.0 ? 0.5 : -0.5)
        );

        long h100 = (long)(humidity_percent * 100.0 + 0.5);
        long p100 = (long)(pressure_hpa * 100.0 + 0.5);

        const char *sign = t100 < 0 ? "-" : "";
        long abs_t100 = t100 < 0 ? -t100 : t100;

        length = snprintf(
            line,
            sizeof(line),
            "{\"device\":\"discovery01\","
            "\"seq\":%lu,\"uptime_ms\":%lu,"
            "\"valid\":true,"
            "\"temperature_c\":%s%ld.%02ld,"
            "\"humidity_percent\":%ld.%02ld,"
            "\"pressure_hpa\":%ld.%02ld,"
            "\"error_count\":%lu}\r\n",
            (unsigned long)attempt_count,
            (unsigned long)last_sample_ms,
            sign,
            abs_t100 / 100L,
            abs_t100 % 100L,
            h100 / 100L,
            h100 % 100L,
            p100 / 100L,
            p100 % 100L,
            (unsigned long)error_count
        );
    }
    else
    {
        /*
         * Failed readings are explicit.
         * Never label old measurements as fresh measurements.
         */
        length = snprintf(
            line,
            sizeof(line),
            "{\"device\":\"discovery01\","
            "\"seq\":%lu,\"uptime_ms\":%lu,"
            "\"valid\":false,"
            "\"temperature_c\":null,"
            "\"humidity_percent\":null,"
            "\"pressure_hpa\":null,"
            "\"driver_result\":%ld,"
            "\"error_count\":%lu}\r\n",
            (unsigned long)attempt_count,
            (unsigned long)HAL_GetTick(),
            (long)driver_result,
            (unsigned long)error_count
        );
    }

    if (length <= 0 || (size_t)length >= sizeof(line))
    {
        usb_records_skipped++;
        return;
    }

    /*
     * Briefly prevent USB interrupts while checking the
     * connection and handing over the persistent buffer.
     * Never overwrite a buffer that is still transmitting.
     */
    uint32_t previous_primask = __get_PRIMASK();
    __disable_irq();

    uint8_t result = USBD_BUSY;

    if (hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED &&
        hUsbDeviceFS.pClassData != NULL)
    {
        USBD_CDC_HandleTypeDef *cdc =
            (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;

        if (cdc->TxState == 0U)
        {
            memcpy(usb_tx_buffer, line, (size_t)length);

            result = CDC_Transmit_FS(
                usb_tx_buffer,
                (uint16_t)length
            );
        }
    }

    __set_PRIMASK(previous_primask);

    if (result == USBD_OK)
    {
        usb_records_queued++;
        /* Blue LED is reserved for acknowledged commands. */
    }
    else
    {
        /*
         * Skip instead of blocking sensor acquisition
         * when the PC is disconnected or USB is busy.
         */
        usb_records_skipped++;
    }
}

int main(void)
{
    HAL_Init();

    SystemClock_Config();
    MX_GPIO_Init();

    HAL_GPIO_WritePin(GPIOD, LED_ALL, GPIO_PIN_SET);
    HAL_Delay(300U);
    HAL_GPIO_WritePin(GPIOD, LED_ALL, GPIO_PIN_RESET);

    MX_I2C1_Init();
    MX_USB_DEVICE_Init();

    HAL_Delay(100U);

    while (1)
    {
        Command_Poll();
        uint32_t cycle_start = HAL_GetTick();

        attempt_count++;
        measurement_valid = 0U;

        HAL_GPIO_TogglePin(GPIOD, LED_ORANGE);

        int8_t result = BME280_OK;

        if (sensor_ready == 0U)
        {
            result = Sensor_Init();

            if (result == BME280_OK)
            {
                sensor_ready = 1U;
            }
        }

        if (result == BME280_OK)
        {
            result = Sensor_Measure();
        }

        driver_result = result;

        if (result == BME280_OK)
        {
            HAL_GPIO_WritePin(
                GPIOD, LED_GREEN, GPIO_PIN_SET
            );

            HAL_GPIO_WritePin(
                GPIOD, LED_RED, GPIO_PIN_RESET
            );
        }
        else
        {
            sensor_ready = 0U;
            measurement_valid = 0U;
            error_count++;

            HAL_GPIO_WritePin(
                GPIOD, LED_GREEN, GPIO_PIN_RESET
            );

            HAL_GPIO_WritePin(
                GPIOD, LED_RED, GPIO_PIN_SET
            );
        }

        Command_Poll();
        USB_SendRecord();

        /*
         * Approximately one acquisition attempt per second.
         * USB interrupts continue during HAL_Delay().
         */
        uint32_t elapsed = HAL_GetTick() - cycle_start;

        if (elapsed < 1000U)
        {
            while ((HAL_GetTick() - cycle_start) < 1000U) {
                Command_Poll();
                HAL_Delay(1U);
            }
        }
    }
}

void SystemClock_Config(void)
{
    RCC_OscInitTypeDef oscillator = {0};
    RCC_ClkInitTypeDef clocks = {0};

    __HAL_RCC_PWR_CLK_ENABLE();

    __HAL_PWR_VOLTAGESCALING_CONFIG(
        PWR_REGULATOR_VOLTAGE_SCALE1
    );

    oscillator.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    oscillator.HSEState = RCC_HSE_ON;

    oscillator.PLL.PLLState = RCC_PLL_ON;
    oscillator.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    oscillator.PLL.PLLM = 8;
    oscillator.PLL.PLLN = 336;
    oscillator.PLL.PLLP = RCC_PLLP_DIV2;
    oscillator.PLL.PLLQ = 7;

    if (HAL_RCC_OscConfig(&oscillator) != HAL_OK)
    {
        Error_Handler();
    }

    clocks.ClockType =
        RCC_CLOCKTYPE_HCLK |
        RCC_CLOCKTYPE_SYSCLK |
        RCC_CLOCKTYPE_PCLK1 |
        RCC_CLOCKTYPE_PCLK2;

    clocks.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clocks.AHBCLKDivider = RCC_SYSCLK_DIV1;
    clocks.APB1CLKDivider = RCC_HCLK_DIV4;
    clocks.APB2CLKDivider = RCC_HCLK_DIV2;

    if (HAL_RCC_ClockConfig(&clocks, FLASH_LATENCY_5) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_I2C1_Init(void)
{
    hi2c1.Instance = I2C1;

    hi2c1.Init.ClockSpeed = 100000;
    hi2c1.Init.DutyCycle = I2C_DUTYCYCLE_2;
    hi2c1.Init.OwnAddress1 = 0;
    hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
    hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
    hi2c1.Init.OwnAddress2 = 0;
    hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
    hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;

    if (HAL_I2C_Init(&hi2c1) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_GPIO_Init(void)
{
    GPIO_InitTypeDef gpio = {0};

    __HAL_RCC_GPIOE_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOH_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();

    /* Deselect the onboard accelerometer. */
    HAL_GPIO_WritePin(GPIOE, GPIO_PIN_3, GPIO_PIN_SET);

    gpio.Pin = GPIO_PIN_3;
    gpio.Mode = GPIO_MODE_OUTPUT_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;

    HAL_GPIO_Init(GPIOE, &gpio);

    /* LEDs off; audio codec held in reset. */
    HAL_GPIO_WritePin(
        GPIOD,
        LED_ALL | GPIO_PIN_4,
        GPIO_PIN_RESET
    );

    gpio.Pin = LED_ALL | GPIO_PIN_4;
    HAL_GPIO_Init(GPIOD, &gpio);

    /*
     * Keep the board's USB host power switch disabled.
     * The PC supplies CN5 VBUS in USB device mode.
     */
    HAL_GPIO_WritePin(GPIOC, GPIO_PIN_0, GPIO_PIN_SET);

    gpio.Pin = GPIO_PIN_0;
    HAL_GPIO_Init(GPIOC, &gpio);

    /*
     * USB pins are initialized by the generated USB
     * low-level code. I2C pins use the generated MSP code.
     */
}

void Error_Handler(void)
{
    __HAL_RCC_GPIOD_CLK_ENABLE();

    GPIO_InitTypeDef gpio = {0};

    gpio.Pin = LED_ALL;
    gpio.Mode = GPIO_MODE_OUTPUT_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;

    HAL_GPIO_Init(GPIOD, &gpio);

    HAL_GPIO_WritePin(GPIOD, LED_ALL, GPIO_PIN_RESET);

    HAL_GPIO_WritePin(
        GPIOD,
        LED_ORANGE | LED_RED,
        GPIO_PIN_SET
    );

    __disable_irq();

    while (1)
    {
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    (void)file;
    (void)line;
    Error_Handler();
}
#endif
