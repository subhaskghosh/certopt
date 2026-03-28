SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS PER
LEFT JOIN ADDRESS AS ADDR
  ON addr.personid = per.personid