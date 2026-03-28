SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON
LEFT JOIN ADDRESS
  ON personid = address.personid